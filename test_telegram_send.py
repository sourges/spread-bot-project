import contextlib
import io
import unittest
from telegram.error import TimedOut, NetworkError, BadRequest
from telegram_send import send_with_retries


class FakeTelegram:
    """Fails with the given errors first, then works. Remembers every try."""

    def __init__(self, errors):
        self.errors = list(errors)
        self.tries = []
        self.sent = []

    async def send(self, message):
        self.tries.append(message)
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(message)


class SendWithRetriesTest(unittest.IsolatedAsyncioTestCase):

    async def run_send(self, fake):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = await send_with_retries(fake.send, "hi", tries=3, wait=0)
        return ok, out.getvalue()

    async def test_works_first_time(self):
        fake = FakeTelegram([])
        ok, out = await self.run_send(fake)
        self.assertTrue(ok)
        self.assertEqual(fake.tries, ["hi"])
        self.assertEqual(fake.sent, ["hi"])
        self.assertEqual(out, "")

    async def test_timeout_then_works(self):
        fake = FakeTelegram([TimedOut()])
        ok, out = await self.run_send(fake)
        self.assertTrue(ok)
        self.assertEqual(len(fake.tries), 2)
        self.assertEqual(fake.sent, ["hi"])
        self.assertIn("retrying in 0s (try 2/3)", out)

    async def test_gives_up_after_3_tries(self):
        fake = FakeTelegram([TimedOut(), NetworkError("down"), TimedOut()])
        ok, out = await self.run_send(fake)
        self.assertFalse(ok)
        self.assertEqual(len(fake.tries), 3)
        self.assertEqual(fake.sent, [])
        self.assertIn("[TELEGRAM ERROR] gave up after 3 tries", out)

    async def test_bad_request_is_not_retried(self):
        # e.g. wrong chat id: trying again won't help
        fake = FakeTelegram([BadRequest("Chat not found")])
        ok, out = await self.run_send(fake)
        self.assertFalse(ok)
        self.assertEqual(len(fake.tries), 1)
        self.assertIn("[TELEGRAM ERROR] Chat not found", out)

    async def test_problems_go_to_log_function(self):
        fake = FakeTelegram([TimedOut()])
        lines = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await send_with_retries(fake.send, "hi", tries=3, wait=0, log=lines.append)
        self.assertEqual(len(lines), 1)
        self.assertIn("retrying in 0s (try 2/3)", lines[0])
        self.assertEqual(out.getvalue(), "")        # nothing printed directly


if __name__ == '__main__':
    unittest.main()
