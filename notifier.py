"""
notifier.py - Telegram notification system for the weather bot.
Sends trade alerts, daily summaries, and error notifications.

Uses python-telegram-bot library with careful async handling to avoid
event loop conflicts when called from threaded code (heartbeat thread,
scheduler thread, etc.).

Daily report includes: balance, trades executed, trades in progress,
trades settled as wins, trades settled as losses, and P&L.
"""

import logging
import asyncio
import threading
from datetime import datetime, timezone

import telegram
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)

# Maximum retries for sending a single Telegram message
_MAX_SEND_RETRIES = 3
_SEND_RETRY_DELAY = 2.0  # seconds


class TelegramNotifier:
    """
    Thread-safe Telegram notifier with daily stat tracking.

    Uses a dedicated event loop running in its own thread so that
    asyncio.run() is never called on the main thread (which can
    conflict with other async code or nested event loops).
    """

    def __init__(self, token: str, chat_id: int):
        self.token = token
        self.chat_id = chat_id
        self.bot = telegram.Bot(token=token)

        # Dedicated event loop for Telegram sends (avoids asyncio conflicts)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="telegram-loop"
        )
        self._thread.start()

        # Lock protects daily_stats from concurrent access
        self._lock = threading.Lock()

        self.daily_stats = {
            "start_balance": 0.0,
            "current_balance": 0.0,
            "trades_entered": 0,
            "trades_skipped": 0,
            "trades_won": 0,
            "trades_lost": 0,
            "trades_in_progress": 0,
            "total_spent": 0.0,
            "start_time": datetime.now(timezone.utc),
        }

    # ------------------------------------------------------------------
    # Low-level send with retry
    # ------------------------------------------------------------------

    def _run_async(self, coro):
        """Schedule a coroutine on the dedicated loop and wait for result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=30)
        except Exception as e:
            logger.error(f"Telegram async call failed: {e}")
            return None

    def send_message(self, text: str) -> bool:
        """
        Send a Telegram message with retry logic.
        Returns True on success, False on failure.
        Thread-safe.
        """
        for attempt in range(1, _MAX_SEND_RETRIES + 1):
            try:
                self._run_async(
                    self.bot.send_message(
                        chat_id=self.chat_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                    )
                )
                logger.info("Telegram message sent successfully")
                return True
            except Exception as e:
                logger.warning(
                    f"Telegram send attempt {attempt}/{_MAX_SEND_RETRIES} failed: {e}"
                )
                if attempt < _MAX_SEND_RETRIES:
                    import time
                    time.sleep(_SEND_RETRY_DELAY * attempt)

        logger.error("Telegram message failed after all retries")
        return False

    # ------------------------------------------------------------------
    # Daily stat management
    # ------------------------------------------------------------------

    def reset_daily(self, current_balance: float) -> None:
        """Reset daily stats for a new trading day. Sends reset notification."""
        with self._lock:
            self.daily_stats = {
                "start_balance": current_balance,
                "current_balance": current_balance,
                "trades_entered": 0,
                "trades_skipped": 0,
                "trades_won": 0,
                "trades_lost": 0,
                "trades_in_progress": 0,
                "total_spent": 0.0,
                "start_time": datetime.now(timezone.utc),
            }

    def record_trade(self, entered: bool = True, size_usdc: float = 0.0) -> None:
        """Record that a trade was entered or skipped."""
        with self._lock:
            if entered:
                self.daily_stats["trades_entered"] += 1
                self.daily_stats["trades_in_progress"] += 1
                self.daily_stats["total_spent"] += size_usdc
            else:
                self.daily_stats["trades_skipped"] += 1

    def record_settlement(self, won: bool) -> None:
        """Record a trade settlement (win or loss)."""
        with self._lock:
            if won:
                self.daily_stats["trades_won"] += 1
            else:
                self.daily_stats["trades_lost"] += 1
            # Decrease in-progress count
            if self.daily_stats["trades_in_progress"] > 0:
                self.daily_stats["trades_in_progress"] -= 1

    def update_balance(self, balance: float) -> None:
        """Update the current known balance."""
        with self._lock:
            self.daily_stats["current_balance"] = balance

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def notify_startup(self, bankroll: float, mode: str, scan_interval: int) -> None:
        """Send bot startup notification."""
        self.send_message(
            f"<b>Bot Started</b>\n"
            f"Mode: <b>{mode}</b>\n"
            f"Bankroll: ${bankroll:.2f} USDC\n"
            f"Scan interval: {scan_interval}min\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    def notify_trade(self, slug: str, city: str, date_str: str,
                     edge: float, size_usdc: float, price: float,
                     prob: float, order_status: str) -> None:
        """Send notification when a trade is placed."""
        status_icon = "+" if order_status in ("simulated", "MATCHED", "LIVE") else "x"
        self.send_message(
            f"<b>[{status_icon}] Trade Placed</b>\n"
            f"Market: {city} {date_str}\n"
            f"Edge: {edge:+.1%} | Prob: {prob:.1%}\n"
            f"Size: ${size_usdc:.2f} @ {price:.3f}\n"
            f"Status: {order_status}\n"
            f"Slug: <code>{slug[:60]}</code>"
        )

    @staticmethod
    def _strip_html(text: str) -> str:
        """Strip HTML tags from text to prevent Telegram parse errors."""
        import re
        # Remove HTML tags and their content for script/style
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        # Remove remaining HTML tags
        text = re.sub(r'<[^>]+>', '', text)
        # Collapse whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def notify_error(self, component: str, error: str) -> None:
        """Send error alert via Telegram. HTML is stripped to prevent parse errors."""
        safe_error = self._strip_html(str(error))[:500]
        self.send_message(
            f"<b>[!] Error in {component}</b>\n"
            f"<code>{safe_error}</code>\n"
            f"Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )

    def notify_cycle_summary(self, trades_placed: int, total_spent: float,
                              markets_scanned: int, elapsed_s: float) -> None:
        """Send a brief cycle completion summary."""
        self.send_message(
            f"<b>Scan Complete</b>\\n"
            f"Markets: {markets_scanned} | Trades: {trades_placed}\\n"
            f"Spent: ${total_spent:.2f} | Time: {elapsed_s:.1f}s"
        )

    def notify_new_weather_market(self, city: str, date_str: str, forecast_temp_c: float,
                                  brackets: list, edge: float, skip_reason: str, slug: str = "") -> None:
        """Send detailed notification when a new weather temperature market hits the sniper websocket.
        Includes available brackets, forecast from weather layer, and exact skip/trade reason.
        This helps debug sniper WS connectivity and strategy logic."""
        brackets_str = ", ".join(str(b) for b in brackets) if brackets else "N/A"
        f_temp = round(forecast_temp_c * 9 / 5 + 32)
        icon = "✅" if any(k in skip_reason.lower() for k in ["trade", "placed", "entered"]) else "⏭️"
        self.send_message(
            f"<b>{icon} New Weather Temp Market via WS</b>\\n"
            f"City/Date: <b>{city} {date_str}</b>\\n"
            f"Forecast High: <b>{forecast_temp_c:.1f}°C / {f_temp}°F</b>\\n"
            f"Brackets: {brackets_str}\\n"
            f"Edge: {edge:+.2%} | Reason: {skip_reason}\\n"
            f"Slug: <code>{slug[:60]}...</code>\\n"
            f"Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
        # Also record as skipped for daily stats (unless it traded)
        if "trade" not in skip_reason.lower():
            self.record_trade(entered=False)

    def send_daily_report(self, end_balance: float) -> None:
        """
        Send the daily summary report.
        Includes balance, P&L, trade counts by status.
        Flags when balance looks like the $200 API fallback.
        """
        _FALLBACK_VAL = 200.0

        with self._lock:
            stats = dict(self.daily_stats)

        start_bal = stats["start_balance"]
        pnl = end_balance - start_bal
        pnl_pct = (pnl / start_bal * 100) if start_bal > 0 else 0.0

        entered = stats["trades_entered"]
        skipped = stats["trades_skipped"]
        won = stats["trades_won"]
        lost = stats["trades_lost"]
        in_progress = stats["trades_in_progress"]
        total_spent = stats["total_spent"]

        # Flag if either balance looks like the API fallback
        start_warning = " (API fallback)" if start_bal == _FALLBACK_VAL else ""
        end_warning = " (API fallback)" if end_balance == _FALLBACK_VAL else ""

        report = (
            f"<b>Daily Weather Bot Report</b>\\n"
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}\\n"
            f"\\n"
            f"<b>Balance</b>\\n"
            f"  Start:   ${start_bal:.2f}{start_warning}\\n"
            f"  End:     ${end_balance:.2f}{end_warning}\\n"
            f"  P&L:     ${pnl:+.2f} ({pnl_pct:+.1f}%)\\n"
            f"\\n"
            f"<b>Trades</b>\\n"
            f"  Executed:    {entered}\\n"
            f"  In Progress: {in_progress}\\n"
            f"  Won:         {won}\\n"
            f"  Lost:        {lost}\\n"
            f"  Skipped:     {skipped}\\n"
            f"  Total Spent: ${total_spent:.2f}\\n"
        )

        self.send_message(report)

    def shutdown(self) -> None:
        """Cleanly stop the async event loop."""
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
        except Exception as e:
            logger.warning(f"Telegram notifier shutdown error: {e}")
