"""A short trading-wisdom banner shown once per login session -- picked
randomly at login time and stashed in the session, so it stays the same
for the whole visit and only changes the next time you log back in
(not on every page load, which would just be noise).

All phrasing here is original -- deliberately not quoting any specific
trader, book, or public figure verbatim. These are plain reminders
about the discipline side of trading (stop-losses, fear, greed,
patience, risk sizing) that the numbers elsewhere on this dashboard
don't enforce for you.
"""
from __future__ import annotations

import random

TRADING_QUOTES = [
    "A stop-loss you don't honor isn't a stop-loss -- it's a suggestion.",
    "Fear sells too early. Greed sells too late. A plan sells on schedule.",
    "The market doesn't know you're right. It only knows where the price is.",
    "Cutting a loss small is a decision. Watching it grow is a habit.",
    "Position size is a risk decision, not a confidence decision.",
    "The trade you didn't take can't hurt you. The one you didn't exit can.",
    "Being right and being profitable are not the same skill.",
    "Averaging down on a broken thesis is just a slower way to be wrong.",
    "A winning streak is not a strategy. It's a sample size of one.",
    "Every open position is a question the market hasn't answered yet.",
    "Greed asks 'how much more.' Discipline asks 'was this the plan.'",
    "The best exit is the one you decided on before you were emotional about it.",
    "Volatility is the price of admission, not a sign you did something wrong.",
    "A trade with no exit plan is a hope, not a position.",
    "Revenge trading is just tuition you pay twice for the same lesson.",
    "The account that survives the bad years compounds through the good ones.",
    "Diversification is admitting you don't know which trade will be the one that hurts.",
    "Patience isn't waiting. It's staying disciplined while you wait.",
    "A stop-loss protects capital. A take-profit protects sanity.",
    "You don't need to catch the top or the bottom -- you need to catch enough of the middle.",
    "If a position keeps you up at night, it's already too big.",
    "The market rewards process, eventually. It punishes impatience, immediately.",
    "Fear of missing out has opened more bad trades than fear of loss ever closed good ones.",
    "Risk what you can afford to be wrong about, not what you hope to be right about.",
    "The hardest trade to take is often the one your plan told you to take anyway.",
]


def random_quote() -> str:
    return random.choice(TRADING_QUOTES)
