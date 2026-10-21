"""Regression tests: a repeated Message-ID must not overwrite a stored mail."""
import asyncio

from mongomock_motor import AsyncMongoMockClient

from src.app.db import storage as storage_mod


# --- 4. insert-only storage ---

def make_storage():
    st = storage_mod.Storage.__new__(storage_mod.Storage)
    st.db = AsyncMongoMockClient()["t"]
    return st


def test_repeated_message_id_does_not_overwrite():
    st = make_storage()
    first = {"message_id": "<same@x>", "to": "a@example.com", "full_body": "original", "dedup_key": "h1:a"}
    second = {"message_id": "<same@x>", "to": "a@example.com", "full_body": "forged", "dedup_key": "h2:a"}

    async def go():
        oid1, c1 = await st.save_mail_once(first)
        oid2, c2 = await st.save_mail_once(second)
        return oid1, c1, oid2, c2, await st.db.mails.count_documents({})

    oid1, c1, oid2, c2, n = asyncio.run(go())
    assert c1 is True and c2 is True and n == 2  # different content: separate documents
    assert oid1 != oid2


def test_same_raw_message_is_deduplicated_and_not_modified():
    st = make_storage()
    a = {"message_id": "<m@x>", "to": "a@example.com", "full_body": "original", "dedup_key": "h:a"}
    b = {**a, "full_body": "changed"}

    async def go():
        o1, c1 = await st.save_mail_once(a)
        o2, c2 = await st.save_mail_once(b)
        doc = await st.db.mails.find_one({"dedup_key": "h:a"})
        return o1, c1, o2, c2, doc["full_body"]

    o1, c1, o2, c2, body = asyncio.run(go())
    assert (c1, c2) == (True, False) and o1 == o2 and body == "original"


def test_save_mail_without_dedup_key_is_insert_only():
    st = make_storage()

    async def go():
        await st.save_mail({"message_id": "<m@x>", "to": "a@b.c", "full_body": "one"})
        await st.save_mail({"message_id": "<m@x>", "to": "a@b.c", "full_body": "two"})
        return (await st.db.mails.find_one({"message_id": "<m@x>"}))["full_body"]

    assert asyncio.run(go()) == "one"


def test_blobs_of_existing_mail_are_not_deleted_on_redelivery():
    import inspect

    from src.app.mail import lmtp

    assert "delete_blobs_for_mails" not in inspect.getsource(lmtp.Handler._store_blobs)
