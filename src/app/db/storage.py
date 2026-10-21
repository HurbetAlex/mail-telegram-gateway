import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from src.app.utils.search import parse_search
from src.app.utils.settings import merge_settings, validate_settings_patch

logger = logging.getLogger(__name__)


def cutoff_oid(days: int, now: Optional[datetime] = None) -> ObjectId:
    """ObjectId boundary of the retention period: anything smaller is older than days days."""
    now = now or datetime.now(timezone.utc)
    return ObjectId.from_datetime(now - timedelta(days=days))


class Storage:
    def __init__(self, uri: str):
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client.mail_gateway

    async def ensure_indexes(self):
        await self.db.mails.create_index("message_id")
        await self.db.mails.create_index("dedup_key")
        await self.db.mails.create_index("status")
        await self.db.deliveries.create_index("mail_id")
        await self.db.blobs.create_index("mail_id")
        await self.db.blobs.create_index("created_at")
        await self.db.sent.create_index("user_id")
        await self._ensure_plain_timestamp_index()
        await self._ensure_feature_indexes()

    async def _ensure_plain_timestamp_index(self):
        """timestamp_1 index without TTL.

        Retention is handled by cleanup_old_mails (by _id, honouring starred mails),
        while a TTL index knows nothing about starred and does not touch the float timestamps of old mails.
        Idempotent; errors are only logged so startup does not fail.
        """
        try:
            info = await self.db.mails.index_information()
            if "expireAfterSeconds" in info.get("timestamp_1", {}):
                await self.db.mails.drop_index("timestamp_1")
                logger.info("TTL index timestamp_1 dropped")
            await self.db.mails.create_index("timestamp")
        except Exception as e:
            logger.warning(f"timestamp index setup failed: {e}")

    async def cleanup_old_mails(self, days: int, batch: int = 500) -> Dict[str, int]:
        """Deletes mails older than days days (except starred) and their deliveries.

        Age is judged by _id only: old mails sometimes have a float timestamp.
        In batches of batch so a weak server is not overloaded.
        """
        cutoff = cutoff_oid(days)
        flt = {"_id": {"$lt": cutoff}, "starred": {"$ne": True}}
        deleted = 0
        removed_ids: List[str] = []
        while True:
            docs = await self.db.mails.find(
                flt, {"_id": 1, "message_id": 1}
            ).limit(batch).to_list(length=batch)
            if not docs:
                break
            ids = [d["_id"] for d in docs]
            res = await self.db.mails.delete_many({"_id": {"$in": ids}})
            deleted += res.deleted_count
            removed_ids.extend(str(i) for i in ids)
            # Deliveries of the old version reference the Message-ID, the new one references str(_id).
            # One Message-ID can belong to several mails (dedup by message_id+to):
            # touch its deliveries only when no mails with it remain.
            mids = {d["message_id"] for d in docs if d.get("message_id")}
            if mids:
                alive = await self.db.mails.find(
                    {"message_id": {"$in": list(mids)}}, {"message_id": 1}
                ).to_list(length=None)
                mids -= {d["message_id"] for d in alive}
            await self.db.deliveries.delete_many(
                {"mail_id": {"$in": [str(i) for i in ids] + list(mids)}}
            )
            if res.deleted_count == 0:
                break  # guard against an infinite loop
        await self._delete_blobs(removed_ids)
        kept = await self.db.mails.count_documents(
            {"_id": {"$lt": cutoff}, "starred": True}
        )
        return {"deleted": deleted, "kept_starred": kept}

    @staticmethod
    async def _delete_blobs(mail_ids: List[str]) -> None:
        """Attachment/HTML files of deleted mails; a failure must not break cleanup."""
        if not mail_ids:
            return
        try:
            from src.app.mail import blobstore  # lazy: blobstore imports storage

            await blobstore.delete_blobs_for_mails(mail_ids)
        except Exception as e:
            logger.warning(f"blob cleanup failed: {e}")

    async def cleanup_candidates_by_user(self, days: int) -> Dict[Optional[int], Dict[str, Any]]:
        """Deletion candidates (older than days days, not starred) grouped by owner:
        {user_id|None: {"n": count, "oldest": datetime, "newest": datetime}}.
        Dates come from _id (generation_time, UTC)."""
        pipeline: List[Dict[str, Any]] = [
            {"$match": {"_id": {"$lt": cutoff_oid(days)}, "starred": {"$ne": True}}},
            {
                "$group": {
                    "_id": "$user_id",
                    "n": {"$sum": 1},
                    "oldest": {"$min": "$_id"},
                    "newest": {"$max": "$_id"},
                }
            },
        ]
        out: Dict[Optional[int], Dict[str, Any]] = {}
        async for row in self.db.mails.aggregate(pipeline):
            out[row["_id"]] = {
                "n": row["n"],
                "oldest": row["oldest"].generation_time,
                "newest": row["newest"].generation_time,
            }
        return out

    async def get_expiring(
        self,
        ttl_days: int,
        user_id: Optional[int] = None,
        skip: int = 0,
        limit: int = 20,
        window_days: int = 7,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Mails that will soon be cleaned up (or already qualify but have not
        been deleted yet): older than ttl_days - window_days, not starred. Oldest first."""
        if ttl_days <= 0:
            return [], 0
        flt: Dict[str, Any] = {
            "_id": {"$lt": cutoff_oid(max(ttl_days - window_days, 0))},
            "starred": {"$ne": True},
        }
        if user_id is not None:
            flt["user_id"] = user_id
        total = await self.db.mails.count_documents(flt)
        mails = (
            await self.db.mails.find(flt)
            .sort("_id", 1)
            .skip(skip)
            .limit(limit)
            .to_list(length=limit)
        )
        return mails, total

    async def count_failed_since(self, since: datetime) -> int:
        """Mails with status failed created after since (by _id)."""
        return await self.db.mails.count_documents(
            {"_id": {"$gte": ObjectId.from_datetime(since)}, "status": "failed"}
        )

    async def get_meta(self, key: str) -> Optional[Dict[str, Any]]:
        """Service document of the meta collection ({_id: key, ...fields}) without _id."""
        doc = await self.db.meta.find_one({"_id": key})
        if not doc:
            return None
        doc.pop("_id", None)
        return doc

    async def set_meta(self, key: str, value: Dict[str, Any]):
        await self.db.meta.update_one({"_id": key}, {"$set": value}, upsert=True)

    # --- Routes ---

    async def get_all_routes(self) -> List[Dict[str, Any]]:
        return await self.db.routes.find({}).to_list(length=1000)

    async def add_route(self, prefix: str, user_id: int):
        await self.db.routes.update_one(
            {"prefix": prefix},
            {"$set": {"prefix": prefix, "user_id": user_id, "disabled": False}},
            upsert=True,
        )

    async def add_domain_route(self, domain: str, user_id: int):
        await self.db.routes.update_one(
            {"domain": domain},
            {"$set": {"domain": domain, "user_id": user_id, "disabled": False}},
            upsert=True,
        )

    async def delete_route(self, prefix: str) -> bool:
        res = await self.db.routes.delete_one({"prefix": prefix})
        return res.deleted_count > 0

    async def delete_domain_route(self, domain: str) -> bool:
        res = await self.db.routes.delete_one({"domain": domain})
        return res.deleted_count > 0

    async def _toggle(self, flt: Dict[str, Any]) -> Optional[bool]:
        doc = await self.db.routes.find_one(flt)
        if not doc:
            return None
        new_disabled = not doc.get("disabled", False)
        await self.db.routes.update_one(
            {"_id": doc["_id"]}, {"$set": {"disabled": new_disabled}}
        )
        return not new_disabled  # True = the route is now enabled

    async def toggle_route(self, prefix: str) -> Optional[bool]:
        return await self._toggle({"prefix": prefix})

    async def toggle_domain_route(self, domain: str) -> Optional[bool]:
        return await self._toggle({"domain": domain})

    async def get_domain_route(self, domain: str) -> Optional[int]:
        doc = await self.db.routes.find_one(
            {"domain": domain, "disabled": {"$ne": True}}
        )
        return doc["user_id"] if doc else None

    # --- Mails ---

    async def save_mail_once(self, mail_data: Dict[str, Any]) -> Tuple[str, bool]:
        """Insert-only save: returns (_id, created).

        Deduplication key: `dedup_key` (hash of the raw message + recipient, computed from
        server-side data) if present, else (message_id, to). An existing document is never
        modified, so a sender who repeats a Message-ID cannot overwrite a stored mail.
        """
        if mail_data.get("dedup_key"):
            flt = {"dedup_key": mail_data["dedup_key"]}
        elif mail_data.get("message_id"):
            flt = {"message_id": mail_data["message_id"], "to": mail_data.get("to")}
        else:
            res = await self.db.mails.insert_one(mail_data)
            return str(res.inserted_id), True
        res = await self.db.mails.update_one(flt, {"$setOnInsert": mail_data}, upsert=True)
        if res.upserted_id is not None:
            return str(res.upserted_id), True
        doc = await self.db.mails.find_one(flt, {"_id": 1})
        return str(doc["_id"]), False

    async def save_mail(self, mail_data: Dict[str, Any]) -> str:
        """Saves a mail (insert-only, see save_mail_once) and returns the string _id."""
        oid, _ = await self.save_mail_once(mail_data)
        return oid

    async def get_mail(self, oid: str) -> Optional[Dict[str, Any]]:
        try:
            return await self.db.mails.find_one({"_id": ObjectId(oid)})
        except (InvalidId, TypeError):
            return None

    async def set_mail_status(self, oid: str, status: str):
        try:
            await self.db.mails.update_one(
                {"_id": ObjectId(oid)}, {"$set": {"status": status}}
            )
        except (InvalidId, TypeError):
            pass

    async def get_unassigned(self) -> List[Dict[str, Any]]:
        return (
            await self.db.mails.find({"status": "unassigned"})
            .sort("_id", -1)
            .to_list(length=50)
        )

    async def get_history(
        self,
        query: str = "",
        skip: int = 0,
        limit: int = 10,
        user_id: Optional[int] = None,
        status: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        flt = build_history_filter(query, user_id, status)
        total = await self.db.mails.count_documents(flt)
        mails = (
            await self.db.mails.find(flt)
            .sort("_id", -1)
            .skip(skip)
            .limit(limit)
            .to_list(length=limit)
        )
        return mails, total

    async def get_stats(self, user_id: Optional[int] = None) -> Dict[str, int]:
        stats: Dict[str, int] = {}
        pipeline: List[Dict[str, Any]] = [{"$group": {"_id": "$status", "n": {"$sum": 1}}}]
        if user_id is not None:
            pipeline.insert(0, {"$match": {"user_id": user_id}})
        async for row in self.db.mails.aggregate(pipeline):
            stats[row["_id"] or "unknown"] = row["n"]
        return stats

    async def get_counts_since(self, since, user_id: Optional[int] = None) -> Dict[str, int]:
        """Counters by status since since; user_id restricts to the owner's mails."""
        counts: Dict[str, int] = {}
        match: Dict[str, Any] = {"timestamp": {"$gte": since}}
        if user_id is not None:
            match["user_id"] = user_id
        async for row in self.db.mails.aggregate(
            [
                {"$match": match},
                {"$group": {"_id": "$status", "n": {"$sum": 1}}},
            ]
        ):
            counts[row["_id"] or "unknown"] = row["n"]
        return counts

    # --- Sender rules (mute) ---

    async def add_rule(self, pattern: str, action: str = "mute", user_id: Optional[int] = None):
        """user_id=None is a global rule (admin), otherwise a personal one."""
        pattern = pattern.lower()
        await self.db.rules.update_one(
            {"pattern": pattern, "user_id": user_id},
            {"$set": {"pattern": pattern, "action": action, "user_id": user_id}},
            upsert=True,
        )

    async def get_rules(self, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """None returns all rules; otherwise global + personal. Every rule has a user_id field."""
        flt: Dict[str, Any] = {}
        if user_id is not None:
            flt = {"$or": [{"user_id": None}, {"user_id": user_id}]}
        rules = await self.db.rules.find(flt).sort("pattern", 1).to_list(length=500)
        for r in rules:
            r.setdefault("user_id", None)
        return rules

    async def delete_rule_by_oid(self, oid: str, user_id: Optional[int] = None) -> bool:
        """user_id=None skips the owner check (admin); otherwise only own rules."""
        _id = self._oid(oid)
        if _id is None:
            return False
        flt: Dict[str, Any] = {"_id": _id}
        if user_id is not None:
            flt["user_id"] = user_id
        res = await self.db.rules.delete_one(flt)
        return res.deleted_count > 0

    async def match_rule(self, sender_addr: str, user_id: Optional[int] = None) -> Optional[str]:
        """Rule for the exact sender address or domain; a personal rule beats a global one."""
        addr = (sender_addr or "").lower()
        if not addr:
            return None
        domain = addr.split("@")[-1]
        flt: Dict[str, Any] = {"pattern": {"$in": [addr, domain]}}
        flt["$or"] = [{"user_id": None}] + ([{"user_id": user_id}] if user_id is not None else [])
        docs = await self.db.rules.find(flt).to_list(length=20)
        return pick_rule(docs, user_id, addr)

    # --- Mail templates ---

    async def add_template(self, user_id: int, name: str, text: str) -> Dict[str, Any]:
        """Creates/updates a template by (user_id, name) and returns the document with _id."""
        return await self.db.templates.find_one_and_update(
            {"user_id": user_id, "name": name},
            {"$set": {"user_id": user_id, "name": name, "text": text}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

    async def get_templates(self, user_id: int) -> List[Dict[str, Any]]:
        return (
            await self.db.templates.find({"user_id": user_id})
            .sort("name", 1)
            .to_list(length=50)
        )

    async def get_template(self, oid: str) -> Optional[Dict[str, Any]]:
        try:
            return await self.db.templates.find_one({"_id": ObjectId(oid)})
        except (InvalidId, TypeError):
            return None

    async def delete_template(self, oid: str) -> bool:
        try:
            res = await self.db.templates.delete_one({"_id": ObjectId(oid)})
            return res.deleted_count > 0
        except (InvalidId, TypeError):
            return False

    # --- Deliveries ---

    async def add_delivery(self, mail_id: str, user_id: int, tg_mid: int):
        await self.db.deliveries.insert_one(
            {
                "mail_id": mail_id,
                "user_id": user_id,
                "tg_message_id": tg_mid,
                "deleted": False,
            }
        )


    # --- Starred, aliases, newsletters, whereis ---

    async def _ensure_feature_indexes(self):
        await self.db.aliases.create_index("address", unique=True)
        await self.db.aliases.create_index("user_id")
        await self.db.mails.create_index("alias")
        await self.db.mails.create_index([("starred", 1), ("user_id", 1)])

    @staticmethod
    def _oid(value: str) -> Optional[ObjectId]:
        try:
            return ObjectId(value)
        except (InvalidId, TypeError):
            return None

    async def set_starred(self, oid: str, starred: bool, user_id: Optional[int] = None) -> bool:
        """user_id=None skips the owner check (admin)."""
        _id = self._oid(oid)
        if _id is None:
            return False
        flt: Dict[str, Any] = {"_id": _id}
        if user_id is not None:
            flt["user_id"] = user_id
        res = await self.db.mails.update_one(flt, {"$set": {"starred": bool(starred)}})
        return res.matched_count > 0

    async def count_starred(self, user_id: Optional[int]) -> int:
        flt: Dict[str, Any] = {"starred": True}
        if user_id is not None:
            flt["user_id"] = user_id
        return await self.db.mails.count_documents(flt)

    async def count_aliases(self, user_id: Optional[int]) -> int:
        return await self.db.aliases.count_documents(
            {} if user_id is None else {"user_id": user_id}
        )

    async def get_starred(
        self, user_id: Optional[int], skip: int = 0, limit: int = 20
    ) -> List[Dict[str, Any]]:
        flt: Dict[str, Any] = {"starred": True}
        if user_id is not None:
            flt["user_id"] = user_id
        return await self.db.mails.find(flt).sort("_id", -1).skip(skip).limit(limit).to_list(length=limit)

    async def set_unsubscribed(self, oid: str) -> None:
        _id = self._oid(oid)
        if _id is None:
            return
        await self.db.mails.update_one(
            {"_id": _id}, {"$set": {"unsubscribed_at": datetime.now(timezone.utc)}}
        )

    # Code masking: mails with a code in the window [newer_than, older_than] that are not yet masked
    async def get_mails_for_otp_mask(
        self, older_than: datetime, newer_than: datetime, limit: int = 100
    ) -> List[Dict[str, Any]]:
        return await self.db.mails.find(
            {
                "otp": {"$nin": [None, ""]},
                "otp_masked": {"$ne": True},
                "mask_at": {"$exists": False},
                "status": "delivered",
                "timestamp": {"$lte": older_than, "$gte": newer_than},
            }
        ).sort("_id", 1).to_list(length=limit)

    async def get_mails_due_for_mask(self, now: datetime, limit: int = 100) -> List[Dict[str, Any]]:
        """Mails with a code that are due to be masked (mask_at <= now), ordered by mask_at."""
        return await self.db.mails.find(build_due_mask_filter(now)).sort("mask_at", 1).to_list(length=limit)

    async def get_deliveries(self, mail_id: str) -> List[Dict[str, Any]]:
        return await self.db.deliveries.find({"mail_id": mail_id}).to_list(length=20)

    async def set_otp_masked(self, oid: str) -> None:
        _id = self._oid(oid)
        if _id is not None:
            await self.db.mails.update_one({"_id": _id}, {"$set": {"otp_masked": True}})

    # Aliases

    async def get_alias(self, address: str) -> Optional[Dict[str, Any]]:
        return await self.db.aliases.find_one({"address": (address or "").lower()})

    async def create_alias(self, user_id: int, address: str, note: str = "") -> Dict[str, Any]:
        doc = {
            "address": address.lower(),
            "user_id": user_id,
            "note": note,
            "disabled": False,
            "created_at": datetime.now(timezone.utc),
        }
        try:
            res = await self.db.aliases.insert_one(doc)
        except DuplicateKeyError:
            raise ValueError(f"Alias {address} already exists")
        doc["_id"] = res.inserted_id
        return doc

    async def list_aliases(self, user_id: Optional[int]) -> List[Dict[str, Any]]:
        """Aliases with mail_count and sender_domains (from mails with the alias field)."""
        flt: Dict[str, Any] = {} if user_id is None else {"user_id": user_id}
        aliases = await self.db.aliases.find(flt).sort("address", 1).to_list(length=500)
        if not aliases:
            return aliases
        stats: Dict[str, Dict[str, Any]] = {
            a["address"]: {"count": 0, "domains": set()} for a in aliases
        }
        async for row in self.db.mails.aggregate(
            [
                {"$match": {"alias": {"$in": list(stats)}}},
                {"$group": {"_id": {"a": "$alias", "f": "$from"}, "n": {"$sum": 1}}},
            ]
        ):
            st = stats.get(row["_id"]["a"])
            if st is None:
                continue
            st["count"] += row["n"]
            domain = parseaddr(str(row["_id"].get("f") or ""))[1].split("@")[-1].lower()
            if domain:
                st["domains"].add(domain)
        for a in aliases:
            st = stats[a["address"]]
            a["mail_count"] = st["count"]
            a["sender_domains"] = sorted(st["domains"])
        return aliases

    def _alias_filter(self, alias_id: str, user_id: Optional[int]) -> Optional[Dict[str, Any]]:
        _id = self._oid(alias_id)
        if _id is None:
            return None
        flt: Dict[str, Any] = {"_id": _id}
        if user_id is not None:
            flt["user_id"] = user_id
        return flt

    async def set_alias_disabled(
        self, alias_id: str, user_id: Optional[int], disabled: bool
    ) -> bool:
        flt = self._alias_filter(alias_id, user_id)
        if flt is None:
            return False
        res = await self.db.aliases.update_one(flt, {"$set": {"disabled": bool(disabled)}})
        return res.matched_count > 0

    async def delete_alias(self, alias_id: str, user_id: Optional[int]) -> bool:
        flt = self._alias_filter(alias_id, user_id)
        if flt is None:
            return False
        res = await self.db.aliases.delete_one(flt)
        return res.deleted_count > 0

    async def whereis(self, query: str, user_id: Optional[int]) -> List[Dict[str, Any]]:
        """Where mails from a sender arrived: groups (recipient, forwarder)."""
        rows = await self.db.mails.aggregate(build_whereis_pipeline(query, user_id)).to_list(length=100)
        return [
            {
                "to": r["_id"].get("to"),
                "forwarded_by": r["_id"].get("via"),
                "count": r["n"],
                "first": r["first"].generation_time,
                "last": r["last"].generation_time,
                "sender": r.get("sender"),
            }
            for r in rows
        ]

    # --- Mail blobs, read state, deletion ---

    async def set_mail_blobs(
        self, oid: str, html_id: Optional[str], attachments: List[Dict[str, Any]]
    ) -> None:
        """References to saved blobs: html_id and attachments [{id,name,mime,size}]."""
        _id = self._oid(oid)
        if _id is None:
            return
        await self.db.mails.update_one(
            {"_id": _id}, {"$set": {"html_id": html_id, "attachments": attachments}}
        )

    async def mark_read(self, oids: List[str], read: bool, user_id: Optional[int] = None) -> int:
        flt = build_ids_filter(oids, user_id)
        if flt is None:
            return 0
        res = await self.db.mails.update_many(flt, {"$set": {"read": bool(read)}})
        return res.matched_count

    async def count_unread(self, user_id: Optional[int] = None) -> int:
        return await self.db.mails.count_documents(build_unread_filter(user_id))

    async def delete_mails(self, oids: List[str], user_id: Optional[int] = None) -> Dict[str, Any]:
        """Deletes mails and their deliveries. Blobs are deleted by the caller (blobstore).

        Returns {"deleted", "deliveries": [{"user_id", "tg_message_id"}], "mail_ids"}.
        """
        out: Dict[str, Any] = {"deleted": 0, "deliveries": [], "mail_ids": []}
        flt = build_ids_filter(oids, user_id)
        if flt is None:
            return out
        docs = await self.db.mails.find(flt, {"_id": 1, "message_id": 1}).to_list(length=None)
        if not docs:
            return out
        ids = [d["_id"] for d in docs]
        str_ids = [str(i) for i in ids]
        res = await self.db.mails.delete_many({"_id": {"$in": ids}})
        # Deliveries of the old version reference the Message-ID: touch them if no mails with it remain
        mids = {d["message_id"] for d in docs if d.get("message_id")}
        if mids:
            alive = await self.db.mails.find(
                {"message_id": {"$in": list(mids)}}, {"message_id": 1}
            ).to_list(length=None)
            mids -= {d["message_id"] for d in alive}
        dflt = {"mail_id": {"$in": str_ids + list(mids)}}
        deliveries = await self.db.deliveries.find(dflt).to_list(length=None)
        await self.db.deliveries.delete_many(dflt)
        out["deleted"] = res.deleted_count
        out["mail_ids"] = str_ids
        out["deliveries"] = [
            {"user_id": d.get("user_id"), "tg_message_id": d.get("tg_message_id")}
            for d in deliveries
            if d.get("tg_message_id") is not None
        ]
        return out

    # --- Newsletters, codes, contacts ---

    async def get_newsletters(self, user_id: Optional[int] = None, limit: int = 200) -> List[Dict[str, Any]]:
        rows = await self.db.mails.aggregate(build_newsletters_pipeline(user_id)).to_list(length=2000)
        rules = await self.get_rules(user_id)
        return merge_newsletter_rows(rows, rules, user_id, limit)

    async def get_codes(self, user_id: Optional[int] = None, days: int = 7, limit: int = 50) -> List[Dict[str, Any]]:
        mails = (
            await self.db.mails.find(build_codes_filter(user_id, days))
            .sort("_id", -1)
            .limit(limit)
            .to_list(length=limit)
        )
        return [
            {
                "mail_id": str(m["_id"]),
                "otp": m.get("otp"),
                "from": m.get("from"),
                "subject": m.get("subject"),
                "at": m["_id"].generation_time,
            }
            for m in mails
        ]

    async def log_sent(
        self, user_id: int, from_addr: str, to: List[str], subject: str, message_id: Optional[str]
    ) -> None:
        await self.db.sent.insert_one(
            {
                "user_id": user_id,
                "from": from_addr,
                "to": [t.lower() for t in to],
                "subject": subject,
                "message_id": message_id,
                "at": datetime.now(timezone.utc),
            }
        )

    async def get_contacts(self, user_id: int, q: str = "", limit: int = 10) -> List[str]:
        """Addresses from the user's sent mail + senders of their mails, by frequency."""
        sent_rows = await self.db.sent.aggregate(
            [
                {"$match": {"user_id": user_id}},
                {"$unwind": "$to"},
                {"$group": {"_id": "$to", "n": {"$sum": 1}}},
                {"$sort": {"n": -1}},
                {"$limit": 500},
            ]
        ).to_list(length=500)
        mail_rows = await self.db.mails.aggregate(
            [
                {"$match": {"user_id": user_id, "status": {"$nin": ["spam", "sent"]}}},
                {"$sort": {"_id": -1}},
                {"$limit": 3000},
                {"$group": {"_id": "$from", "n": {"$sum": 1}}},
                {"$sort": {"n": -1}},
                {"$limit": 500},
            ]
        ).to_list(length=500)
        return merge_contacts(sent_rows, mail_rows, q, limit)

    # --- User settings ---

    async def get_settings(self, user_id: int) -> Dict[str, Any]:
        doc = await self.db.user_settings.find_one({"_id": user_id})
        return merge_settings(doc)

    async def set_settings(self, user_id: int, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Validates and saves a partial update; ValueError on invalid data."""
        clean = validate_settings_patch(patch)
        if clean:
            await self.db.user_settings.update_one(
                {"_id": user_id}, {"$set": clean}, upsert=True
            )
        return await self.get_settings(user_id)

    async def get_digest_users(self, user_ids, now: Optional[datetime] = None, window_minutes: int = 1) -> List[int]:
        """Which of user_ids are due for a digest (enabled and local time = digest_time).
        Call once a minute; window_minutes is the window width."""
        from src.app.utils.settings import digest_due

        ids = list(user_ids)
        docs = {
            d["_id"]: d
            for d in await self.db.user_settings.find({"_id": {"$in": ids}}).to_list(length=None)
        }
        return [
            uid for uid in ids if digest_due(merge_settings(docs.get(uid)), now, window_minutes)
        ]

    async def get_recipient_addresses(
        self, user_id: int, domains, limit: int = 10
    ) -> List[str]:
        """Addresses (lowercase) on the given domains that mails reached for the user
        (alias or To), by frequency. Spam, blocked and
        the user's own sent mail are not counted."""
        rows = await self.db.mails.aggregate(
            [
                {"$match": {"user_id": user_id, "status": {"$nin": ["spam", "blocked", "sent"]}}},
                {"$sort": {"_id": -1}},
                {"$limit": 3000},
                {"$group": {"_id": {"$toLower": {"$ifNull": ["$alias", "$to"]}}, "n": {"$sum": 1}}},
                {"$sort": {"n": -1}},
                {"$limit": 200},
            ]
        ).to_list(length=200)
        doms = {d.lower() for d in domains}
        out = [
            r["_id"] for r in rows
            if isinstance(r.get("_id"), str) and r["_id"].rpartition("@")[2] in doms
        ]
        return out[:limit]

    async def get_sent_from_addresses(self, user_id: int, limit: int = 10) -> List[str]:
        """Addresses the user sent from (the sent collection), by frequency."""
        rows = await self.db.sent.aggregate(
            [
                {"$match": {"user_id": user_id}},
                {"$group": {"_id": {"$toLower": "$from"}, "n": {"$sum": 1}, "last": {"$max": "$at"}}},
                {"$sort": {"n": -1, "last": -1}},
                {"$limit": limit},
            ]
        ).to_list(length=limit)
        return [r["_id"] for r in rows if isinstance(r.get("_id"), str)]


# --- Pure filter and pipeline functions (covered by tests/test_data_v2.py) ---

# Statuses whose mails are not counted as "unread" (never delivered to the user)
UNREAD_EXCLUDED_STATUSES = ["spam", "muted", "blocked"]


def _rx(value: str) -> Dict[str, Any]:
    return {"$regex": re.escape(value), "$options": "i"}


def build_history_filter(
    query: str = "", user_id: Optional[int] = None, status: Optional[str] = None
) -> Dict[str, Any]:
    """Mongo filter for get_history: search operators + free text (section 5 of the contract)."""
    p = parse_search(query or "")
    clauses: List[Dict[str, Any]] = []
    if p["text"]:
        rx = _rx(p["text"])
        clauses.append(
            {"$or": [{"from": rx}, {"to": rx}, {"orig_to": rx}, {"subject": rx}, {"full_body": rx}]}
        )
    for v in p["from"]:
        clauses.append({"from": _rx(v)})
    for v in p["to"]:
        rx = _rx(v)
        clauses.append({"$or": [{"to": rx}, {"orig_to": rx}, {"alias": rx}]})
    for v in p["alias"]:
        clauses.append({"alias": _rx(v)})
    if p["has_code"]:
        clauses.append({"otp": {"$nin": [None, ""]}})
    if p["has_attachment"]:
        clauses.append({"attachments.0": {"$exists": True}})
    if p["is_unread"]:
        clauses.append({"read": False})
    if p["is_starred"]:
        clauses.append({"starred": True})
    if p["is_bulk"]:
        clauses.append({"bulk": True})
    if p["after"]:
        clauses.append({"_id": {"$gte": ObjectId.from_datetime(p["after"])}})
    if p["before"]:
        clauses.append({"_id": {"$lt": ObjectId.from_datetime(p["before"])}})
    if user_id is not None:
        clauses.append({"user_id": user_id})
    if status:
        clauses.append({"status": status})
    return {"$and": clauses} if clauses else {}


def build_due_mask_filter(now: datetime) -> Dict[str, Any]:
    return {
        "otp": {"$nin": [None, ""]},
        "otp_masked": {"$ne": True},
        "status": "delivered",
        "mask_at": {"$lte": now},
    }


def build_ids_filter(oids: List[str], user_id: Optional[int]) -> Optional[Dict[str, Any]]:
    """Filter "mails by id list (+ owner)"; None if there are no valid ids."""
    ids = []
    for o in oids or []:
        if not isinstance(o, str):  # ObjectId(None) would create a new random id
            continue
        try:
            ids.append(ObjectId(o))
        except (InvalidId, TypeError):
            continue
    if not ids:
        return None
    flt: Dict[str, Any] = {"_id": {"$in": ids}}
    if user_id is not None:
        flt["user_id"] = user_id
    return flt


def build_unread_filter(user_id: Optional[int]) -> Dict[str, Any]:
    """Unread = read: false (mails without the field count as read), excluding spam/muted/blocked."""
    flt: Dict[str, Any] = {"read": False, "status": {"$nin": UNREAD_EXCLUDED_STATUSES}}
    if user_id is not None:
        flt["user_id"] = user_id
    return flt


def build_codes_filter(
    user_id: Optional[int], days: int = 7, now: Optional[datetime] = None
) -> Dict[str, Any]:
    flt: Dict[str, Any] = {
        "otp": {"$nin": [None, ""]},
        "_id": {"$gte": cutoff_oid(days, now)},
        "status": {"$nin": ["spam", "blocked"]},
    }
    if user_id is not None:
        flt["user_id"] = user_id
    return flt


def build_newsletters_pipeline(user_id: Optional[int]) -> List[Dict[str, Any]]:
    """Groups bulk mails by the raw "From"; merging by address is merge_newsletter_rows."""
    match: Dict[str, Any] = {"bulk": True, "status": {"$nin": ["spam", "blocked"]}}
    if user_id is not None:
        match["user_id"] = user_id
    return [
        {"$match": match},
        {"$sort": {"_id": -1}},
        {
            "$group": {
                "_id": "$from",
                "n": {"$sum": 1},
                "latest": {"$first": "$_id"},
                "unsubscribe": {"$first": "$unsubscribe"},
                "unsubscribed_at": {"$max": "$unsubscribed_at"},
            }
        },
        {"$sort": {"n": -1}},
        {"$limit": 2000},
    ]


def merge_newsletter_rows(
    rows: List[Dict[str, Any]], rules: List[Dict[str, Any]], user_id: Optional[int], limit: int = 200
) -> List[Dict[str, Any]]:
    """Merges groups by sender address; muted follows the mute rules (global + own)."""
    muted: set = set()
    for r in rules:
        if r.get("action") == "mute" and r.get("user_id") in (None, user_id):
            muted.add(str(r.get("pattern", "")).lower())
    by_addr: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        name, addr = parseaddr(str(r["_id"] or ""))
        addr = addr.lower()
        if not addr:
            continue
        item = by_addr.get(addr)
        if item is None:
            item = by_addr[addr] = {
                "sender": addr,
                "name": name or addr.split("@")[0],
                "domain": addr.split("@")[-1],
                "count": 0,
                "latest_oid": None,
                "unsubscribe": None,
                "unsubscribed": False,
            }
        item["count"] += r["n"]
        if r.get("unsubscribed_at"):
            item["unsubscribed"] = True
        if item["latest_oid"] is None or r["latest"] > item["latest_oid"]:
            item["latest_oid"] = r["latest"]
            item["unsubscribe"] = r.get("unsubscribe")
            if name:
                item["name"] = name
    out = []
    for item in by_addr.values():
        oid = item.pop("latest_oid")
        item["last"] = oid.generation_time
        item["latest_mail_id"] = str(oid)
        item["muted"] = item["sender"] in muted or item["domain"] in muted
        out.append(item)
    out.sort(key=lambda x: (-x["count"], x["sender"]))
    return out[:limit]


def merge_contacts(
    sent_rows: List[Dict[str, Any]], mail_rows: List[Dict[str, Any]], q: str, limit: int = 10
) -> List[str]:
    """Combines sent and incoming addresses by total frequency; filter by substring."""
    counts: Dict[str, int] = {}
    for r in sent_rows:
        addr = str(r["_id"] or "").strip().lower()
        if addr:
            counts[addr] = counts.get(addr, 0) + r["n"]
    for r in mail_rows:
        addr = parseaddr(str(r["_id"] or ""))[1].strip().lower()
        if addr:
            counts[addr] = counts.get(addr, 0) + r["n"]
    needle = (q or "").strip().lower()
    items = [(a, n) for a, n in counts.items() if needle in a]
    items.sort(key=lambda x: (-x[1], x[0]))
    return [a for a, _ in items[:limit]]


def pick_rule(docs: List[Dict[str, Any]], user_id: Optional[int], addr: str) -> Optional[str]:
    """Picks an action from the matching rules: a personal rule beats a global one,
    an exact address beats a domain."""
    addr = (addr or "").lower()

    def rank(d: Dict[str, Any]):
        own = user_id is not None and d.get("user_id") == user_id
        return (0 if own else 1, 0 if d.get("pattern") == addr else 1)

    ok = [d for d in docs if d.get("user_id") is None or (user_id is not None and d.get("user_id") == user_id)]
    if not ok:
        return None
    return min(ok, key=rank)["action"]


def build_whereis_pipeline(query: str, user_id: Optional[int], limit: int = 50) -> List[Dict[str, Any]]:
    match: Dict[str, Any] = {"from": {"$regex": re.escape(query), "$options": "i"}}
    if user_id is not None:
        match["user_id"] = user_id
    return [
        {"$match": match},
        {
            "$group": {
                "_id": {"to": {"$ifNull": ["$orig_to", "$to"]}, "via": "$forwarded_by"},
                "n": {"$sum": 1},
                "first": {"$min": "$_id"},
                "last": {"$max": "$_id"},
                "sender": {"$last": "$from"},
            }
        },
        {"$sort": {"last": -1}},
        {"$limit": limit},
    ]


# Singleton, initialized in main()
db: Optional[Storage] = None


def init_db(uri: str):
    global db
    db = Storage(uri)
