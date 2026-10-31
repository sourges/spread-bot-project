import asyncio
from telegram.error import BadRequest, NetworkError, RetryAfter


async def send_with_retries(send, message, tries=3, wait=5, log=print):
    """Send one Telegram message, trying again if the connection fails.

    send:  an async function that sends `message` (e.g. bot.send_message with
           the chat id filled in).
    Retries on network problems and time-outs (TimedOut is a NetworkError),
    and when Telegram says "slow down" (RetryAfter). Other errors, like a
    wrong chat id, won't fix themselves, so they are not retried.
    Never raises: returns True if sent, False if it gave up.
    log:   where problem lines go (print by default; the grid also writes
           them to grid_errors.txt).

    Note: if Telegram got the message but the reply timed out, the retry
    sends it again, so once in a while an alert can arrive twice."""
    for attempt in range(1, tries + 1):
        try:
            await send(message)
            return True
        except BadRequest as e:
            # BadRequest is a kind of NetworkError in python-telegram-bot,
            # so catch it first: a bad message or chat id won't fix itself.
            log(f"[TELEGRAM ERROR] {e}")
            return False
        except (NetworkError, RetryAfter) as e:
            if attempt == tries:
                log(f"[TELEGRAM ERROR] gave up after {tries} tries: {e}")
                return False
            log(f"[TELEGRAM] {e} - retrying in {wait}s (try {attempt + 1}/{tries})")
            await asyncio.sleep(wait)
        except Exception as e:
            log(f"[TELEGRAM ERROR] {e}")
            return False
