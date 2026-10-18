# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Outcome labels, at the start of a skip reason or failure message. tools/hil_report.py counts
them on their own rather than as skipped or failed.

DNS: in a board's dns_tests (farm.toml), never started because the board does not come up
reliably (the Feather STM32F405's crystal load caps).
DNF: started, then the board stopped answering, so the test never finished.
"""
DNS = "DNS (did not start): "
DNF = "DNF (did not finish): "
