"""Backtesting and the time machine.

Everything here replays history *point in time*: at each decision moment the
bot only sees bars that had closed, news that had been published, and the
quote that was live at that moment. See engine.py for the rules.
"""
