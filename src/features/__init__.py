"""Four-lens feature extractors: flow, structure, context, intent.

Backtest replay extensions (Phase 0 Track C): ``replay.FeatureReplayer`` walks
candle DataFrames and emits ``FeaturePack`` snapshots per bar. The auxiliary
lenses ``order_flow``, ``vol_regime``, ``correlation`` extend the per-bar
context for the simulator. ``cache.FeatureCache`` memoises packs across runs.
"""
