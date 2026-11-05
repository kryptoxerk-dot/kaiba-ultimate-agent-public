"""Binance USD-M perpetuals for the Kaiba Ultimate Agent (KAIBA-ALL-CRYPTO-PROGRAM-20261103).

Kept apart from the GMGN on-chain execution path on purpose: ``d1_breakout`` is the frozen strategy,
``binance_public`` reads keyless market data, ``paper`` is the forward paper book. Nothing in this
package holds a key, places an order or moves funds.
"""
