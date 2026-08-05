"""Meta-labeling: the single highest-leverage ML upgrade available here.

THE IDEA (López de Prado, AFML ch. 3). Do not ask one model to decide both
*which way* and *whether to bet*. Split it:

    PRIMARY   (your three lenses + the LLM strategists) decides the SIDE.
    SECONDARY (this module) decides WHETHER TO TAKE IT, and how big.

Why this works so well in practice: the primary model can have mediocre
precision and still be valuable, as long as it has decent recall — because the
secondary model filters out its bad calls. You get to keep an aggressive signal
generator AND a conservative trigger. Empirically meta-labeling lifts F1
substantially over trying to make one model do both jobs, and it is the standard
way quant shops turn a "promising but noisy" signal into a tradeable one.

It also fits this architecture almost too neatly: the Ledger ALREADY stores, for
every signal, the full feature snapshot at emission plus the graded outcome.
That is a supervised dataset sitting there, fully labelled, waiting.

THREE THINGS DONE PROPERLY, because the standard implementations get them wrong:

  1. PURGED + EMBARGOED CV. Trades overlap in time. A naive train/test split
     leaks: a test-set trade whose outcome window overlaps a training trade
     shares information with it. Purging removes overlapping training samples;
     the embargo drops a buffer after each test fold. Without this your
     cross-validated accuracy is a fiction and you will deploy a model that
     looked great and isn't.

  2. SAMPLE UNIQUENESS WEIGHTING. Overlapping labels are not independent
     observations. Five signals sharing one outcome window should not count as
     five pieces of evidence. Weighting by average uniqueness fixes the effective
     sample size.

  3. HONEST ABSTENTION. If there is not enough data, the model says so and the
     engine falls back to the primary. A secondary model trained on 40 samples
     that confidently vetoes signals is worse than no secondary model at all.

Implementation is a small gradient-boosted stump ensemble in pure Python — no
sklearn dependency, fully deterministic, and inspectable. If you later want
sklearn/LightGBM the interface stays identical.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------- samples

@dataclass
class MetaSample:
    """One graded signal: features at emission, outcome, and its time window."""
    features: Dict[str, float]
    label: int                 # 1 = the primary signal was worth taking
    t_start_ns: int
    t_end_ns: int
    weight: float = 1.0
    seat_id: str = ""
    realized_r: float = 0.0


def average_uniqueness(samples: Sequence[MetaSample]) -> List[float]:
    """Fraction of each label's window that is NOT shared with other labels.

    Concurrency is computed on the union of event boundaries so it is exact
    rather than bucketed. A sample overlapping four others contributes ~1/5 of
    the evidence a lone sample does -- which is the honest accounting.
    """
    n = len(samples)
    if n == 0:
        return []
    edges = sorted({s.t_start_ns for s in samples} |
                   {s.t_end_ns for s in samples})
    if len(edges) < 2:
        return [1.0] * n
    seg = list(zip(edges[:-1], edges[1:]))
    conc = [0] * len(seg)
    for s in samples:
        for i, (a, b) in enumerate(seg):
            if s.t_start_ns <= a and b <= s.t_end_ns:
                conc[i] += 1

    out = []
    for s in samples:
        tot = 0.0
        span = 0.0
        for i, (a, b) in enumerate(seg):
            if s.t_start_ns <= a and b <= s.t_end_ns and conc[i] > 0:
                w = b - a
                tot += w / conc[i]
                span += w
        out.append(tot / span if span > 0 else 1.0)
    return out


def purged_kfold(samples: Sequence[MetaSample], k: int = 5,
                 embargo_pct: float = 0.01) -> List[Tuple[List[int], List[int]]]:
    """Purged K-fold with embargo. THE correct CV for overlapping financial labels.

    Returns (train_idx, test_idx) per fold. Any training sample whose outcome
    window overlaps the test window is PURGED, and a further embargo buffer is
    dropped after the test block to kill serial-correlation leakage.
    """
    n = len(samples)
    if n < k * 2:
        return []
    order = sorted(range(n), key=lambda i: samples[i].t_start_ns)
    fold_size = n // k
    span = max(s.t_end_ns for s in samples) - min(s.t_start_ns for s in samples)
    embargo_ns = int(span * embargo_pct)

    folds = []
    for f in range(k):
        lo = f * fold_size
        hi = n if f == k - 1 else (f + 1) * fold_size
        test = order[lo:hi]
        if not test:
            continue
        t0 = min(samples[i].t_start_ns for i in test)
        t1 = max(samples[i].t_end_ns for i in test) + embargo_ns

        train = []
        for i in order:
            if i in set(test):
                continue
            s = samples[i]
            # purge: any overlap with the test window at all
            if s.t_end_ns >= t0 and s.t_start_ns <= t1:
                continue
            train.append(i)
        if train and test:
            folds.append((train, test))
    return folds


# ---------------------------------------------------------------- model

@dataclass
class Stump:
    feature: str
    threshold: float
    left: float
    right: float

    def predict(self, x: Dict[str, float]) -> float:
        v = x.get(self.feature, 0.0)
        return self.left if v <= self.threshold else self.right


class GradientStumps:
    """Small gradient-boosted decision stumps on log-odds. Deterministic.

    Depth-1 trees are a deliberate choice, not a limitation: with the sample
    sizes a live trading engine accumulates in its first months, deeper trees
    overfit spectacularly. Stumps + shrinkage is the honest capacity for this
    data regime, and it stays fully interpretable — you can read every split.
    """

    def __init__(self, n_rounds: int = 60, lr: float = 0.1,
                 min_leaf_weight: float = 3.0):
        self.n_rounds = n_rounds
        self.lr = lr
        self.min_leaf_weight = min_leaf_weight
        self.stumps: List[Stump] = []
        self.base = 0.0
        self.feature_names: List[str] = []
        self.importance: Dict[str, float] = {}

    @staticmethod
    def _sigmoid(z: float) -> float:
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-min(z, 40)))
        e = math.exp(max(z, -40))
        return e / (1.0 + e)

    def fit(self, X: List[Dict[str, float]], y: List[int],
            w: Optional[List[float]] = None) -> "GradientStumps":
        n = len(X)
        if n == 0:
            return self
        w = w or [1.0] * n
        wsum = sum(w) or 1.0
        pos = sum(wi for wi, yi in zip(w, y) if yi == 1)
        p0 = min(max(pos / wsum, 1e-6), 1 - 1e-6)
        self.base = math.log(p0 / (1 - p0))
        self.feature_names = sorted({k for x in X for k in x})
        self.importance = {f: 0.0 for f in self.feature_names}

        F = [self.base] * n
        for _ in range(self.n_rounds):
            p = [self._sigmoid(f) for f in F]
            grad = [(y[i] - p[i]) * w[i] for i in range(n)]
            hess = [max(p[i] * (1 - p[i]) * w[i], 1e-6) for i in range(n)]

            best = None
            for feat in self.feature_names:
                vals = sorted({x.get(feat, 0.0) for x in X})
                if len(vals) < 2:
                    continue
                # quantile candidate splits: cheap and robust to outliers
                cands = [vals[int(len(vals) * q)]
                         for q in (0.2, 0.35, 0.5, 0.65, 0.8)
                         if int(len(vals) * q) < len(vals)]
                for thr in sorted(set(cands)):
                    gl = hl = gr = hr = 0.0
                    for i, x in enumerate(X):
                        if x.get(feat, 0.0) <= thr:
                            gl += grad[i]; hl += hess[i]
                        else:
                            gr += grad[i]; hr += hess[i]
                    if hl < self.min_leaf_weight or hr < self.min_leaf_weight:
                        continue
                    gain = gl * gl / hl + gr * gr / hr
                    if best is None or gain > best[0]:
                        best = (gain, feat, thr, gl / hl, gr / hr)
            if best is None:
                break
            gain, feat, thr, lv, rv = best
            st = Stump(feat, thr, self.lr * lv, self.lr * rv)
            self.stumps.append(st)
            self.importance[feat] = self.importance.get(feat, 0.0) + gain
            for i, x in enumerate(X):
                F[i] += st.predict(x)
        tot = sum(self.importance.values()) or 1.0
        self.importance = {k: round(v / tot, 4)
                           for k, v in sorted(self.importance.items(),
                                              key=lambda kv: -kv[1])}
        return self

    def predict_proba(self, x: Dict[str, float]) -> float:
        z = self.base + sum(s.predict(x) for s in self.stumps)
        return self._sigmoid(z)


# ---------------------------------------------------------------- pipeline

@dataclass
class MetaReport:
    trained: bool
    n_samples: int
    effective_n: float
    cv_auc: float
    cv_precision: float
    cv_recall: float
    base_rate: float
    lift: float
    top_features: Dict[str, float]
    verdict: str

    def to_dict(self) -> Dict[str, object]:
        return {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in self.__dict__.items()}


class MetaLabeler:
    """Trains on the ledger, then gates live signals.

    Fails SAFE: until it has enough evidence and demonstrates out-of-sample lift
    over the base rate, `should_take()` returns True for everything and says why.
    A veto model that hasn't earned the right to veto is just a random signal
    filter wearing a lab coat.
    """

    def __init__(self, *, min_samples: int = 150, min_lift: float = 0.04,
                 threshold: float = 0.5, k_folds: int = 5):
        self.min_samples = min_samples
        self.min_lift = min_lift
        self.threshold = threshold
        self.k_folds = k_folds
        self.model: Optional[GradientStumps] = None
        self.report: Optional[MetaReport] = None

    # ---- feature extraction ---------------------------------------------

    @staticmethod
    def features_from_signal(rec: Dict[str, object]) -> Dict[str, float]:
        """Everything knowable AT EMISSION. Nothing from the outcome — that
        would be the leak that makes a useless model look brilliant."""
        reg = rec.get("regime", {}) or {}
        f: Dict[str, float] = {
            "conviction": float(rec.get("conviction", 0) or 0),
            "prefilter_score": float(rec.get("prefilter_score", 0) or 0),
            "edge_after_cost": float(rec.get("edge_after_cost", 0) or 0),
            "atr_pct": float(reg.get("atr_pct", 0) or 0),
            "rvol": float(reg.get("rvol", 1) or 1),
            "horizon_min": float(rec.get("horizon_min", 45) or 45),
            "n_confluence": float(len(rec.get("confluence_with", []) or [])),
            "is_long": 1.0 if rec.get("direction") == "long" else 0.0,
            "shelf_weight": float(rec.get("shelf_weight", 0) or 0),
        }
        entry = float(rec.get("entry", 0) or 0)
        stop = float(rec.get("stop", 0) or 0)
        risk = abs(entry - stop)
        f["risk_abs"] = risk
        f["risk_atr"] = risk / max(float(reg.get("atr_pct", 0.05) or 0.05)
                                   * max(entry, 1) / 100.0, 1e-6)
        tg = rec.get("targets", []) or []
        if tg and risk > 0:
            f["tp1_r"] = abs(float(tg[0].get("price", entry)) - entry) / risk
        vol = str(reg.get("vol_band", "unknown"))
        for band in ("low", "normal", "high"):
            f[f"vol_{band}"] = 1.0 if vol == band else 0.0
        sess = str(rec.get("liquidity_session", ""))
        for s in ("ASIA", "LONDON", "NY_OVERLAP", "NY_LATE"):
            f[f"sess_{s}"] = 1.0 if sess == s else 0.0
        trend = str(reg.get("trend_htf", "unknown"))
        f["trend_aligned"] = 1.0 if (
            (trend == "up" and rec.get("direction") == "long") or
            (trend == "down" and rec.get("direction") == "short")) else 0.0
        for k in ("vpin", "kyle_lambda", "amihud", "dist_to_vpoc",
                  "in_value_area", "momentum_regime"):
            if k in rec:
                f[k] = float(rec.get(k) or 0)
        return f

    @staticmethod
    def samples_from_ledger(pairs: Sequence[Dict[str, object]]
                            ) -> List[MetaSample]:
        """`pairs` is Ledger.resolved_pairs(). Label = was it worth taking."""
        out: List[MetaSample] = []
        for row in pairs:
            sig = row.get("signal", {}) or {}
            out_rec = row.get("outcome", {}) or {}
            r = float(out_rec.get("realized_r", 0) or 0)
            t0 = int(sig.get("t_ns", 0) or 0)
            secs = int(out_rec.get("time_to_outcome_s", 0) or 0)
            if not t0:
                continue
            out.append(MetaSample(
                features=MetaLabeler.features_from_signal(sig),
                label=1 if r > 0 else 0,
                t_start_ns=t0, t_end_ns=t0 + max(secs, 60) * 1_000_000_000,
                seat_id=str(sig.get("seat_id", "")), realized_r=r))
        return out

    # ---- training --------------------------------------------------------

    def fit(self, samples: List[MetaSample]) -> MetaReport:
        n = len(samples)
        if n < self.min_samples:
            self.report = MetaReport(
                False, n, 0.0, 0.5, 0.0, 0.0, 0.0, 0.0, {},
                f"ABSTAIN: {n} samples < {self.min_samples} required. The "
                f"engine will take every primary signal until the secondary "
                f"model has earned the right to veto.")
            return self.report

        uniq = average_uniqueness(samples)
        for s, u in zip(samples, uniq):
            s.weight = u
        eff_n = sum(uniq)

        folds = purged_kfold(samples, self.k_folds)
        if not folds:
            self.report = MetaReport(False, n, eff_n, 0.5, 0, 0, 0, 0, {},
                                     "ABSTAIN: purged CV produced no usable "
                                     "folds (labels overlap too heavily).")
            return self.report

        aucs, precs, recs = [], [], []
        for train_idx, test_idx in folds:
            m = GradientStumps()
            m.fit([samples[i].features for i in train_idx],
                  [samples[i].label for i in train_idx],
                  [samples[i].weight for i in train_idx])
            probs = [m.predict_proba(samples[i].features) for i in test_idx]
            ys = [samples[i].label for i in test_idx]
            aucs.append(_auc(probs, ys))
            p, r = _precision_recall(probs, ys, self.threshold)
            precs.append(p)
            recs.append(r)

        base = sum(s.label for s in samples) / n
        cv_prec = sum(precs) / len(precs)
        lift = cv_prec - base

        self.model = GradientStumps()
        self.model.fit([s.features for s in samples],
                       [s.label for s in samples],
                       [s.weight for s in samples])

        trained = lift >= self.min_lift and sum(aucs) / len(aucs) > 0.53
        verdict = (
            f"ACTIVE: out-of-sample precision {cv_prec:.1%} vs a {base:.1%} base "
            f"rate (+{lift:.1%} lift, AUC {sum(aucs)/len(aucs):.3f}) on "
            f"{eff_n:.0f} effective samples. Purged CV, so this is not leakage."
            if trained else
            f"ABSTAIN: precision {cv_prec:.1%} vs base {base:.1%} is only "
            f"{lift:+.1%} lift — below the {self.min_lift:.0%} bar. The filter "
            f"has not proven it adds anything, so it will not be allowed to "
            f"veto signals.")

        self.report = MetaReport(
            trained, n, eff_n, sum(aucs) / len(aucs), cv_prec,
            sum(recs) / len(recs), base, lift,
            dict(list(self.model.importance.items())[:8]), verdict)
        return self.report

    # ---- inference -------------------------------------------------------

    def should_take(self, signal_rec: Dict[str, object]
                    ) -> Tuple[bool, float, str]:
        """-> (take, probability, reason). Fails open."""
        if not (self.report and self.report.trained and self.model):
            return True, 0.5, ("secondary model abstaining — "
                               + (self.report.verdict if self.report
                                  else "not yet trained"))
        f = self.features_from_signal(signal_rec)
        p = self.model.predict_proba(f)
        if p >= self.threshold:
            return True, p, f"secondary model p={p:.2f} >= {self.threshold}"
        return False, p, (f"secondary model p={p:.2f} < {self.threshold} — the "
                          f"primary lens is right about direction often enough, "
                          f"but this particular setup's context historically "
                          f"loses. Vetoed.")


def _auc(scores: Sequence[float], labels: Sequence[int]) -> float:
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    if not pos or not neg:
        return 0.5
    wins = ties = 0
    for a in pos:
        for b in neg:
            if a > b:
                wins += 1
            elif a == b:
                ties += 1
    return (wins + 0.5 * ties) / (len(pos) * len(neg))


def _precision_recall(scores: Sequence[float], labels: Sequence[int],
                      thr: float) -> Tuple[float, float]:
    tp = sum(1 for s, y in zip(scores, labels) if s >= thr and y == 1)
    fp = sum(1 for s, y in zip(scores, labels) if s >= thr and y == 0)
    fn = sum(1 for s, y in zip(scores, labels) if s < thr and y == 1)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    return prec, rec
