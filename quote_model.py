"""quote_model.py -- fitted competitor and P(win) model for the Spread level-8 dispatcher.

Built from the 210-world harvested level-8 archive (data/L8) with leakage discipline
(whole worlds held out for validation).  Public API used by dispatch v8:

    from quote_model import QuoteModel, OnlineCompetitor, DAY, HOUR

    qm = QuoteModel.load()                        # reads data/fits/quotes.json, cheap
    qm.slope                                      # logit slope on log(price/list), fitted 9.07
    qm.prior_log_level, qm.prior_log_level_sd     # world level prior (log), 0.36 +- 0.09

    road = ...                                    # anything with .km(a,b) and .minutes(a,b)
    ctx  = qm.context(request, road)              # per-request static features (dict)
    qm.lane_log_factor("helsinki", "oulu")        # persistent lane factor (log), 0 if unknown
    qm.trust_log(trust)                           # log acceptance multiplier from trust count
    qm.adapt_offset(dealer_share)                 # log comp shift from our past win share at a dealer

    oc = OnlineCompetitor(qm)                     # one per world; prior from the archive
    p  = oc.p_win(ctx, prices, trust)             # P(win request) for prices {kind: cents}
    pt = oc.p_take(ctx, prices, trust)            # {kind: P(win AND dealer takes that option)}
    oc.observe(ctx, prices, won, at, trust)       # feed each quote outcome, in request order
    oc.log_level, oc.level, oc.level_sd           # current world-level posterior
    oc.war_active, oc.war_log_odds                # level-8 price-war detector
    oc.p_win_now(ctx, prices, trust, at)          # same as p_win but advances the day first

The model is a logistic model of the dealer's choice:
    P(win) = sigmoid( b * (log_comp - log(price/list)) + A0 )
    log_comp = level + day + lane + trust_ratio*log(1+0.01*trust) + adapt(dealer win share)
prices are the quote for the dealer's fastest (first) option; quote every option of the
dealer's fastest kind genuinely when using p_win (the archive only identifies that regime).
p_take additionally uses a conditional-logit model of which option the dealer picks among
our quotes (fitted on won requests): U_kind = theta_sd*[same_day] + theta_w3*[within_3_days]
+ theta_logx*log(p/list) + theta_price*p_euros.
"""
from __future__ import annotations

import json
import math
import os

OPTION_KINDS = ("same_day", "next_day", "within_3_days")
DAY = 24 * 60
HOUR = 60
DEFAULT_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fits", "quotes.json")
_PI8 = math.pi / 8.0


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def logit(p: float) -> float:
    return math.log(p / (1.0 - p))


class QuoteModel:
    """Fitted competitor / P(win) model.  Load once and share between threads/worlds."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.slope = float(spec["slope"])
        self.trust_ratio = float(spec["trust_ratio"])
        self.prior_log_level = float(spec["prior"]["log_level_mean"])
        self.prior_log_level_sd = float(spec["prior"]["log_level_sd"])
        self.day_sd = float(spec["day"]["sd"])
        self.war_log_shift = float(spec["day"]["war_log_shift"])
        self.war_prior = float(spec["day"].get("war_prior", 0.2))
        self.war_hours = tuple(spec["day"].get("war_hours", (8, 18)))
        self.lane_factor = dict(spec["lane_factor"])
        self.lane_fallback = spec.get("lane_fallback", {})
        self.A = spec["A"]
        self.adapt_edges = list(spec["adapt"]["edges"])
        self.adapt_offsets = {int(k): float(v) for k, v in spec["adapt"]["offsets_log_comp"].items()}
        self.choice_spec = list(spec["choice"]["spec"])
        self.choice_theta = [float(spec["choice"]["theta"][k]) for k in self.choice_spec]
        self.option_accept_slope = float(spec["option_accept"]["slope"]) if spec.get("option_accept", {}).get("slope") else self.slope
        menu = spec.get("menu", {})
        self.menu_rel_nd = float(menu.get("rel_next_day", 0.0))
        self.menu_rel_w3 = float(menu.get("rel_within_3_days", 0.0))
        self._A_num = dict(self.A["numeric"])
        self._A_dealer = dict(self.A["dealer"])
        self._A_hour = dict(self.A["hourbin"])
        self._A_kinds = dict(self.A["kinds"])
        self._A_ngen = dict(self.A["n_genuine"])
        self._A_fastest = dict(self.A["fastest"])

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str | None = None) -> "QuoteModel":
        with open(path or DEFAULT_MODEL_PATH) as fh:
            return cls(json.load(fh))

    # ------------------------------------------------------------------ lane factor
    def lane_log_factor(self, pickup: str, delivery: str, km: float | None = None) -> float:
        """Persistent part of the competitor's lane factor (log); 0 for an average lane."""
        v = self.lane_factor.get(pickup + ">" + delivery)
        if v is not None:
            return v
        fb = self.lane_fallback
        if not fb:
            return 0.0
        v = fb["const"] + fb["origin"].get(pickup, 0.0) + fb["dest"].get(delivery, 0.0)
        if km is not None:
            v += fb["km_per_100km"] * km / 100.0
        return v

    # ------------------------------------------------------------------ request context
    def context(self, request: dict, road) -> dict:
        """Static features for one PriceRequest.  road has .km(a,b) and .minutes(a,b)."""
        opts = request["options"]
        fastest = opts[0]["kind"]
        if fastest not in OPTION_KINDS:
            fastest = opts[0]["kind"]
        lists = {o["kind"]: o["listPriceCents"] / 100.0 for o in opts}
        deadlines = {o["kind"]: o["deadline"] for o in opts}
        legs = request["legs"]
        p0 = legs[0]["pickup"]["placeId"]
        d0 = legs[0]["delivery"]["placeId"]
        kms = [road.km(l["pickup"]["placeId"], l["delivery"]["placeId"]) for l in legs]
        mins = [road.minutes(l["pickup"]["placeId"], l["delivery"]["placeId"]) for l in legs]
        lanes = [(l["pickup"]["placeId"], l["delivery"]["placeId"]) for l in legs]
        list_fast = lists[fastest]
        at = request["arrivedAt"]
        n_legs = len(legs)
        premium = any(l["car"].get("premium") for l in legs)
        electric = any(l["car"].get("electric") for l in legs)
        low_charge = any((l["car"].get("chargePercent") is not None and l["car"].get("chargePercent") < 30) for l in legs)
        private_any = any(l["pickup"]["privateAddress"] or l["delivery"]["privateAddress"] for l in legs)
        ref = request["referenceFeeCents"] / 100.0
        hour = (at % DAY) / HOUR
        hourbin = 0 if hour < 10 else 1 if hour < 12 else 2 if hour < 14 else 3 if hour < 16 else 4
        lane_lf = self.lane_log_factor(*lanes[0], km=kms[0])
        ctx = dict(
            request_id=request["id"], dealer=request["dealerId"], at=at, respond_by=request["respondBy"],
            fastest=fastest, kinds="+".join(o["kind"] for o in opts), n_options=len(opts),
            lists=lists, deadlines=deadlines, ref=ref, n_legs=n_legs,
            km=sum(kms), minutes=sum(mins), lanes=lanes, lane_log_factor=lane_lf,
            premium=float(premium), electric=float(electric), low_charge=float(low_charge),
            private_any=float(private_any), has_note=float(request.get("note") is not None),
            hourbin=hourbin, hour=hour,
            ref_over_list=math.log(max(ref, 1e-9) / list_fast),
            loglist=math.log(list_fast),
            km100=sum(kms) / 100.0, min100=sum(mins) / 100.0,
            slack=(deadlines[fastest] - at - 30 * n_legs) / 60.0,
            ref_cents=int(request["referenceFeeCents"]),
        )
        return ctx

    # ------------------------------------------------------------------ fixed adjustments
    def A_value(self, ctx: dict, n_quoted: int | None = None) -> float:
        """Everything except level/day/lane/trust/adaptation and the price term, in log-comp units."""
        A = self.A
        v = float(A["const"])
        for k, c in self._A_num.items():
            v += c * float(ctx.get(k, 0.0))
        v += self._A_dealer.get(ctx["dealer"], 0.0)
        v += self._A_hour.get(str(ctx["hourbin"]), 0.0)
        v += self._A_kinds.get(ctx["kinds"], 0.0)
        if n_quoted is not None:
            v += self._A_ngen.get(str(n_quoted), 0.0)
        v += self._A_fastest.get(ctx["fastest"], 0.0)
        return v

    def trust_log(self, trust: int) -> float:
        """Log acceptance multiplier from the dealer trust count (rules: +1% per counted order)."""
        return self.trust_ratio * math.log1p(0.01 * max(0, int(trust)))

    def adapt_offset(self, dealer_share: float | None) -> float:
        """Log comp shift from our cumulative win share at this dealer (dealer-level state)."""
        if dealer_share is None or dealer_share != dealer_share:
            return self.adapt_offsets.get(-1, 0.0)
        for i, edge in enumerate(self.adapt_edges[1:], start=0):
            if dealer_share <= edge:
                return self.adapt_offsets.get(i, 0.0)
        return self.adapt_offsets.get(len(self.adapt_edges) - 2, 0.0)

    # ------------------------------------------------------------------ probabilities
    def log_comp(self, ctx: dict, level: float, day: float = 0.0, trust: int = 0,
                 dealer_share: float | None = None, dealer_bias: float = 0.0, war_shift: float = 0.0) -> float:
        return (level + day + ctx["lane_log_factor"] + self.trust_log(trust)
                + self.adapt_offset(dealer_share) + dealer_bias + war_shift)

    def _price_ref(self, ctx: dict, prices: dict) -> tuple[str, float] | None:
        """The option whose price drives P(win): the dealer's fastest (first) genuine quote."""
        for kind in (ctx["fastest"],) + OPTION_KINDS:
            if prices.get(kind) is not None:
                p = prices[kind] / 100.0
                lst = ctx["lists"].get(kind)
                if lst:
                    return kind, p / lst
        return None

    def menu_correction(self, ctx: dict, prices: dict, kind: str, x: float) -> float:
        """Logit correction for slower genuinely-quoted options (in log-ratio units)."""
        corr = 0.0
        for k, coef in (("next_day", self.menu_rel_nd), ("within_3_days", self.menu_rel_w3)):
            if k == kind or prices.get(k) is None:
                continue
            lst = ctx["lists"].get(k)
            if not lst:
                continue
            corr += coef * math.log((prices[k] / 100.0 / lst) / x)
        return corr

    def p_win(self, ctx: dict, prices: dict, log_comp: float, n_quoted: int | None = None,
              var: float = 0.0) -> float:
        """P(the dealer buys this request from us) for a menu of prices {kind: cents}.

        The dealer's fastest genuinely-quoted option drives the price response; slower options
        enter through the relative-price correction fitted on multi-option quotes.
        """
        ref = self._price_ref(ctx, prices)
        if ref is None:
            return 0.0
        kind, x = ref
        nq = n_quoted if n_quoted is not None else sum(1 for k in prices if prices.get(k) is not None)
        corr = self.menu_correction(ctx, prices, kind, x)
        z = self.slope * (log_comp + self.A_value(ctx, nq) - math.log(x)) + corr
        if var > 0:
            z = z / math.sqrt(1.0 + _PI8 * self.slope * self.slope * var)
        return sigmoid(z)

    # ------------------------------------------------------------------ option choice
    def _choice_utility(self, ctx: dict, kind: str, price_cents: float) -> float:
        u = 0.0
        x = price_cents / 100.0 / ctx["lists"][kind]
        features = dict(is_sd=1.0 if kind == "same_day" else 0.0,
                        is_w3=1.0 if kind == "within_3_days" else 0.0,
                        logx=math.log(x), price=price_cents / 100.0,
                        x=x, price_rel=price_cents / 100.0 / ctx["lists"][kind])
        for name, th in zip(self.choice_spec, self.choice_theta):
            u += th * features[name]
        return u

    def p_choice(self, ctx: dict, prices: dict) -> dict:
        """P(dealer takes option k | the request was won), softmax over our genuine quotes."""
        kinds = [k for k in OPTION_KINDS if prices.get(k) is not None]
        if not kinds:
            return {}
        us = [self._choice_utility(ctx, k, prices[k]) for k in kinds]
        m = max(us)
        es = [math.exp(u - m) for u in us]
        tot = sum(es)
        return {k: e / tot for k, e in zip(kinds, es)}

    def p_take(self, ctx: dict, prices: dict, log_comp: float, n_quoted: int | None = None,
               var: float = 0.0) -> dict:
        """{kind: P(deal is won AND the dealer takes that option)}."""
        pw = self.p_win(ctx, prices, log_comp, n_quoted=n_quoted, var=var)
        return {k: pw * v for k, v in self.p_choice(ctx, prices).items()}


    # ------------------------------------------------------------------ convenience
    def p_win_ratio(self, ratio: float, lane_key: str | None = None, log_comp: float = 0.0,
                    features: dict | None = None, trust: int = 0, n_quoted: int = 1) -> float:
        """P(win) for one price ratio = price/list on the dealer's fastest option.

        ratio: our price / list for the dealer's fastest option.
        lane_key: "pickup>delivery" (or "p0>d0|p1>d1" for a trade-in).
        log_comp: level + day from OnlineCompetitor (log).  features: optional dict with
        keys ref_over_list, loglist, km100, min100, slack, premium, electric, low_charge,
        has_note, n_legs, private_any, dealer, hourbin, kinds, fastest.
        """
        if ratio <= 0:
            return 0.0
        f = dict(features or {})
        lk = lane_key or ""
        lane = self.lane_log_factor_key(lk) if lk else f.get("lane_log_factor", 0.0)
        A = self.A_value(f, n_quoted) if f else 0.0
        z = self.slope * (log_comp + lane + self.trust_log(trust) + A - math.log(ratio))
        return sigmoid(z)

    def lane_log_factor_key(self, lane_key: str) -> float:
        """Persistent lane factor for a lane key; trade-in keys ('a>b|c>d') add their legs."""
        if "|" in lane_key:
            return sum(self.lane_log_factor(*part.split(">", 1)) for part in lane_key.split("|"))
        a, _, c = lane_key.partition(">")
        return self.lane_log_factor(a, c)

    def optimal_ratio(self, effective_log_comp: float, cost_ratio: float, late_cost_frac: float = 0.0,
                      grid: tuple | None = None) -> float:
        """x = price/list that maximises expected margin (x*(1-late_cost_frac) - cost_ratio)*P(win).

        effective_log_comp: log_comp + A + lane + trust (everything except -log x).
        cost_ratio: fee/list (or any per-list-unit cost); late_cost_frac: expected late refund share.
        """
        grid = grid or tuple(round(0.50 + 0.02 * i, 2) for i in range(151))
        best, bestx = -1e18, grid[0]
        for x in grid:
            z = self.slope * (effective_log_comp - math.log(x))
            win = sigmoid(z)
            ev = (x * (1.0 - late_cost_frac) - cost_ratio) * win
            if ev > best:
                best, bestx = ev, x
        return bestx


class OnlineCompetitor:
    """One per world: Bayesian filter for the world's effective competitor level and day factor,
    the level-8 price-war detector, and the dealer-level win-share state."""

    def __init__(self, model: QuoteModel, prior_mu: float | None = None, prior_sd: float | None = None,
                 day_sd: float | None = None, war_prior: float | None = None, war_threshold: float = math.log(10.0)):
        self.m = model
        self.mu = float(model.prior_log_level if prior_mu is None else prior_mu)
        self.var = float((model.prior_log_level_sd if prior_sd is None else prior_sd) ** 2)
        self.day_mu0 = 0.0
        self.day_var0 = float((model.day_sd if day_sd is None else day_sd) ** 2)
        self.day_mu = 0.0
        self.day_var = self.day_var0
        self.war_prior = float(model.war_prior if war_prior is None else war_prior)
        self.war_threshold = float(war_threshold)
        self.war_llr = 0.0
        self.war_active = False
        self.last_day = None
        self.dealer_wins: dict[str, int] = {}
        self.dealer_reqs: dict[str, int] = {}
        self.dealer_bias: dict[str, float] = {}
        self._b2 = model.slope * model.slope

    # -- dealer state ------------------------------------------------------
    def dealer_share(self, dealer: str) -> float | None:
        n = self.dealer_reqs.get(dealer, 0)
        if n == 0:
            return None
        return self.dealer_wins.get(dealer, 0) / n

    def _note_outcome(self, dealer: str, won: bool) -> None:
        self.dealer_reqs[dealer] = self.dealer_reqs.get(dealer, 0) + 1
        if won:
            self.dealer_wins[dealer] = self.dealer_wins.get(dealer, 0) + 1

    # -- day state ---------------------------------------------------------
    def _advance(self, at: int) -> None:
        day = int(at) // DAY
        if day != self.last_day:
            self.last_day = day
            self.day_mu = 0.0
            self.day_var = self.day_var0
            self.war_llr = 0.0
            self.war_active = False

    # -- public state ------------------------------------------------------
    @property
    def log_level(self) -> float:
        return self.mu

    @property
    def level(self) -> float:
        return math.exp(self.mu)

    @property
    def level_sd(self) -> float:
        return math.sqrt(self.var)

    @property
    def log_comp(self) -> float:
        return self.mu + self.day_mu

    def war_probability(self) -> float:
        return sigmoid(logit(self.war_prior) + self.war_llr)

    # -- probability wrappers ---------------------------------------------
    def log_comp_for(self, ctx: dict, trust: int = 0) -> float:
        share = self.dealer_share(ctx["dealer"])
        return self.m.log_comp(ctx, self.mu, self.day_mu, trust, share, self.dealer_bias.get(ctx["dealer"], 0.0),
                               self.m.war_log_shift if self.war_active else 0.0)

    def p_win_now(self, ctx: dict, prices: dict, trust: int = 0, at: int | None = None,
                  n_quoted: int | None = None) -> float:
        if at is not None:
            self._advance(at)
        return self.p_win(ctx, prices, trust, n_quoted=n_quoted)

    def p_win(self, ctx: dict, prices: dict, trust: int = 0, n_quoted: int | None = None) -> float:
        return self.m.p_win(ctx, prices, self.log_comp_for(ctx, trust), n_quoted=n_quoted,
                            var=self.var + self.day_var)

    def p_take(self, ctx: dict, prices: dict, trust: int = 0, n_quoted: int | None = None) -> dict:
        return self.m.p_take(ctx, prices, self.log_comp_for(ctx, trust), n_quoted=n_quoted,
                             var=self.var + self.day_var)

    def p_win_ratio(self, ratio: float, lane_key: str, features: dict | None = None,
                    trust: int = 0, n_quoted: int = 1) -> float:
        """Shortcut: P(win) for one ratio on the dealer's fastest option with the current posterior."""
        f = dict(features or {})
        lane = self.m.lane_log_factor_key(lane_key)
        A = self.m.A_value(f, n_quoted) if f else 0.0
        share = self.dealer_share(f.get("dealer", "")) if "dealer" in f else None
        lc = self.mu + self.day_mu + lane + self.m.trust_log(trust) + self.m.adapt_offset(share) \
            + (self.m.war_log_shift if self.war_active else 0.0) + A
        return self.m.p_win_ratio(ratio, None, lc, None, 0, n_quoted)

    def choice_probabilities(self, ctx: dict, prices: dict) -> dict:
        """P(dealer takes option k | the request is won) for a menu."""
        return self.m.p_choice(ctx, prices)

    # -- filtering ---------------------------------------------------------
    def observe(self, ctx: dict, prices: dict, won: bool, at: int, trust: int = 0,
                n_quoted: int | None = None) -> float:
        """Feed one realised quote outcome (in chronological order). Returns the pre-update P(win)."""
        self._advance(at)
        ref = self.m._price_ref(ctx, prices)
        if ref is None:
            return 0.0
        kind, x = ref
        nq = n_quoted if n_quoted is not None else sum(1 for k in prices if prices.get(k) is not None)
        share = self.dealer_share(ctx["dealer"])
        base = (ctx["lane_log_factor"] + self.m.trust_log(trust) + self.m.adapt_offset(share)
                + self.dealer_bias.get(ctx["dealer"], 0.0) + self.m.A_value(ctx, nq)
                + (self.m.war_log_shift if self.war_active else 0.0))
        b = self.m.slope
        logx = math.log(x)
        p = sigmoid(b * (self.mu + self.day_mu + base - logx))
        # --- price-war evidence: H0 day ~ N(0, day_var) vs H1 day ~ N(war_shift, day_var)
        f = math.sqrt(1.0 + _PI8 * self._b2 * self.day_var)
        p0 = sigmoid(b * (self.mu + self.day_mu + base - logx) / f)
        p1 = sigmoid(b * (self.mu + self.day_mu + base + self.m.war_log_shift - logx) / f)
        y = 1.0 if won else 0.0
        self.war_llr += math.log(p1 if won else 1.0 - p1) - math.log(p0 if won else 1.0 - p0)
        if not self.war_active and self.war_llr > self.war_threshold:
            self.war_active = True
        # --- joint Gaussian (level, day) update: prior precision diag(1/var, 1/day_var),
        #     observation gradient b*[1, 1]; posterior precision += b^2*p(1-p)*ones.
        h = self._b2 * p * (1.0 - p)
        iv, idv = 1.0 / self.var, 1.0 / self.day_var
        q11, q22, q12 = iv + h, idv + h, h
        det = q11 * q22 - q12 * q12
        c11, c22, c12 = q22 / det, q11 / det, -q12 / det
        yres = y - p
        self.mu += b * yres * (c11 + c12)
        self.day_mu += b * yres * (c12 + c22)
        self.var, self.day_var = c11, c22
        self._note_outcome(ctx["dealer"], bool(won))
        return p


__all__ = ["QuoteModel", "OnlineCompetitor", "DAY", "HOUR", "OPTION_KINDS", "sigmoid", "logit"]