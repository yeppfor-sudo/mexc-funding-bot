name: funding-backtest

on:
  workflow_dispatch:
    inputs:
      days:
        description: 'Days to backtest'
        default: '30'
      start_balance:
        description: 'Starting balance in USDT'
        default: '100'

jobs:
  backtest:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install requests
      - run: python backtest.py
        env:
          DAYS: ${{ github.event.inputs.days }}
          START_BALANCE: ${{ github.event.inputs.start_balance }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: trades
          path: trades.csv
