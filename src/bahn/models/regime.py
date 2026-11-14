"""Online competitor model for level 8: a Bayesian price-level filter with a price-war mixture and an
outage detector. Self-contained (math only); the dispatcher feeds it outcomes and asks it for prices.

The competitor's log price for a request, relative to the list price, is modelled as

    log comp = m + d_today + lane + log(trust boost) + outage(request, now)

  m        world price level, learned over the week (Gaussian, small random walk between days);
  d_today  the day factor, new each day: N(0, sd_day^2) on a normal day, N(log 0.8, sd_day^2) on the
           price-war day. Two Kalman (Laplace/EKF) filters run side by side for today, one per
           hypothesis, weighted by how well each predicts our wins and losses;
  outage   +log(1.3) for pickups in the outage region from 08:00 to 18:00 (dealers there compare us
           with a backup ~30% dearer). For each candidate region (a town + places within 80 km) we keep
           today's log-likelihood ratio of "down here" against "nowhere down"; the prior favours busy
           towns and the days left in the week.

P(win) = sigmoid(slope * (log comp - log(price / list))).

    reg = Regime(places)                       # places: [{"id", "lat", "lon"}, ...] from the map
    reg.observe(request, lr, won)              # lr: our cheapest log(price/list) net of lane and trust
    log_comp = reg.level(now) + reg.adjust(request, now)
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

DAY = 1440
WIN_FROM, WIN_TO = 8 * 60, 18 * 60
# Requests per week by pickup town over 15 saved level-8 worlds: "busy regions go down more often".
BUSY = {"vantaa": 195, "hyvinkaa": 191, "espoo": 77, "helsinki": 67, "lahti": 48, "oulu": 47, "raisio": 40,
        "lempaala": 40, "jyvaskyla": 36, "turku": 36, "tampere": 36, "kuopio": 25, "vaasa": 23, "pori": 23,
        "porvoo": 20, "ylivieska": 18, "rovaniemi": 16, "mikkeli": 15, "kokkola": 12, "kouvola": 11,
        "lappeenranta": 10, "hameenlinna": 8, "salo": 8, "seinajoki": 8, "joensuu": 7, "rauma": 6, "kemi": 6,
        "kotka": 3, "savonlinna": 2, "kajaani": 2}


def _sig(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _haversine(a, b) -> float:
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


class _Kalman2:
    """Gaussian over (m, d) with logistic observations z = slope * (m + d + off - lr), h = (1, 1)."""
    __slots__ = ("m", "d", "pmm", "pmd", "pdd")

    def __init__(self, m, d, pmm, pmd, pdd):
        self.m, self.d, self.pmm, self.pmd, self.pdd = m, d, pmm, pmd, pdd

    def mean_var(self):
        return self.m + self.d, self.pmm + 2 * self.pmd + self.pdd

    def predict(self, x: float, slope: float, ev: float = 0.0) -> float:
        """P(win) for x = off - lr, integrating the level uncertainty (probit approximation)."""
        mu, v = self.mean_var()
        return _sig(slope * (mu + x) / math.sqrt(1 + math.pi * slope * slope * (v + ev) / 8))

    def update(self, x: float, won: bool, slope: float, ev: float = 0.0) -> tuple:
        """EKF step; ev is the variance of the per-request effects (the caller updates those)."""
        mu, v = self.mean_var()
        p = _sig(slope * (mu + x))
        g, h = slope * ((1.0 if won else 0.0) - p), slope * slope * p * (1 - p)
        a, b = self.pmm + self.pmd, self.pmd + self.pdd       # P h
        k = h / (1 + h * (v + ev))
        self.pmm -= k * a * a
        self.pmd -= k * a * b
        self.pdd -= k * b * b
        ga, gb = self.pmm + self.pmd, self.pmd + self.pdd     # P_new h
        c = 1 + h * ev
        self.m += ga * g / c
        self.d += gb * g / c
        return g, h, v


class Regime:
    def __init__(self, places: Iterable[dict], level0: float = math.log(1.37), slope: float = 12.0,
                 sd_level0: float = 0.07, sd_day: float = 0.05, q_level: float = 0.02,
                 pw_shift: float = math.log(0.8), outage_shift: float = math.log(1.3), radius_km: float = 80.0,
                 busy: Optional[dict] = None, pw_detect: float = 0.8, outage_detect: float = 0.8,
                 outage_mode: str = "mean", outage_on: float = 0.5, first_day: int = 0, last_day: int = 4,
                 lo: float = math.log(1.0), hi: float = math.log(1.9), pw_floor: float = 0.05,
                 pw_cap: float = 0.7, outage_cap: float = 0.7, effects: Optional[dict] = None,
                 q_effect: float = 0.03, warm_n: int = 0, warm_q: float = 0.0):
        self.slope, self.sd_day, self.q_level = slope, sd_day, q_level
        self.pw_shift, self.outage_shift = pw_shift, outage_shift
        self.pw_detect, self.outage_detect = pw_detect, outage_detect
        self.outage_mode, self.outage_on = outage_mode, outage_on
        self.first_day, self.last_day = first_day, last_day
        self.lo, self.hi = lo, hi
        self.pw_floor, self.pw_cap, self.outage_cap = pw_floor, pw_cap, outage_cap
        # Per-request effects new in each world: {"pickup"|"delivery"|"dealer": prior sd}, kept over the week
        # with a random walk of q_effect per day ({} turns them off; dealer effects did not validate).
        self.eff_sd = dict(effects) if effects is not None else {"pickup": 0.03, "delivery": 0.03}
        self.q_effect = q_effect
        # Warm-up: for the session's first warm_n outcomes, extra random-walk variance on the world level before each
        # update, warm_q^2 * (1 - n / warm_n): a learning rate that starts high and decays to today's (0 = off).
        self.warm_n, self.warm_q, self.n_obs = warm_n, warm_q, 0
        self.eff: dict = {}
        pl = {p["id"]: (p["lat"], p["lon"]) for p in places}
        busy = busy or BUSY
        self.regions = {c: frozenset(p for p in pl if _haversine(pl[c], pl[p]) <= radius_km) for c in pl}
        tot = sum(busy.get(c, 1.0) for c in pl)
        self.w_center = {c: busy.get(c, 1.0) / tot for c in pl}
        self.centers_of = {p: [c for c in pl if p in self.regions[c]] for p in pl}
        self.m, self.vm = level0, sd_level0 ** 2
        self.day: Optional[int] = None
        self.pw_done = self.outage_done = False
        self.history: list[dict] = []          # one summary per finished day
        self._new_day(first_day)

    # ---------- days ----------
    def _prior_pw(self, day: int) -> float:
        if day > self.last_day:
            return 0.0
        if self.pw_done:                        # a false alarm must not blind us to the real war
            return self.pw_floor
        if self.first_day > 0:                  # joined mid-week: the war may be behind us
            return 0.2
        return min(self.pw_cap, 1.0 / max(1, self.last_day - day + 1))

    def _prior_outage(self, day: int) -> float:
        if day > self.last_day:
            return 0.0
        if self.outage_done:
            return 0.02
        if self.first_day > 0:
            return 0.2
        return min(self.outage_cap, 1.0 / max(1, self.last_day - day + 1))

    def _new_day(self, day: int) -> None:
        if self.day is not None:
            # Collapse the two hypotheses into the world level; the day factor does not carry over.
            fs, ws = self.filters, self.wts
            m = sum(w * f.m for w, f in zip(ws, fs))
            vm = sum(w * (f.pmm + (f.m - m) ** 2) for w, f in zip(ws, fs))
            pout, center = self.outage_posterior()
            self.history.append(dict(day=self.day, level=m, p_pw=ws[1], p_outage=pout, center=center, n=self.n_today))
            if ws[1] > self.pw_detect:
                self.pw_done = True
            if pout > self.outage_detect:
                self.outage_done = True
            self.m, self.vm = m, vm + self.q_level ** 2 * max(1, day - self.day)
            for e in self.eff.values():
                e[1] += self.q_effect ** 2
        self.day = day
        vd = self.sd_day ** 2
        self.filters = [_Kalman2(self.m, 0.0, self.vm, 0.0, vd), _Kalman2(self.m, self.pw_shift, self.vm, 0.0, vd)]
        ppw = self._prior_pw(day)
        self.wts = [1 - ppw, ppw]
        self.p_out_day = self._prior_outage(day)
        self.llr = {c: 0.0 for c in self.regions}
        self.n_today = 0

    def _roll(self, now: int) -> None:
        d = now // DAY
        if self.day is None or d > self.day:
            self._new_day(d)

    # ---------- outage ----------
    def outage_posterior(self) -> tuple[float, Optional[str]]:
        """(P(some region is down today), most likely center)."""
        if self.p_out_day <= 0:
            return 0.0, None
        best, bc, tot = 0.0, None, 0.0
        mx = max(self.llr.values())
        for c, l in self.llr.items():
            v = self.w_center[c] * math.exp(l - mx)
            tot += v
            if v > best:
                best, bc = v, c
        num = self.p_out_day * tot
        den = num + (1 - self.p_out_day) * math.exp(-mx)
        return num / den, bc

    def p_down(self, place: str) -> float:
        """P(the competitor is down for pickups at this place today)."""
        if self.p_out_day <= 0 or place not in self.centers_of:
            return 0.0
        mx = max(self.llr.values())
        tot = sum(self.w_center[c] * math.exp(l - mx) for c, l in self.llr.items())
        here = sum(self.w_center[c] * math.exp(self.llr[c] - mx) for c in self.centers_of[place])
        num = self.p_out_day * tot
        den = num + (1 - self.p_out_day) * math.exp(-mx)
        return (num / den) * here / tot

    @staticmethod
    def in_window(t: int) -> bool:
        return WIN_FROM <= t % DAY < WIN_TO

    def adjust(self, request: dict, now: int) -> float:
        """Log-price offset to add to the competitor level for this request (the outage), at minute now."""
        self._roll(now)
        em = self.effect(request)[0]
        if not self.in_window(now):
            return em
        p = self.p_down(request["legs"][0]["pickup"]["placeId"])
        if self.outage_mode == "switch":
            return em + (self.outage_shift if p >= self.outage_on else 0.0)
        return em + p * self.outage_shift

    # ---------- price level ----------
    def level(self, now: int) -> float:
        """Today's log competitor level (x list, net of lane and trust): world level + day factor."""
        self._roll(now)
        mu = sum(w * (f.m + f.d + self._pw_off(k, now)) for k, (w, f) in enumerate(zip(self.wts, self.filters)))
        return min(self.hi, max(self.lo, mu))

    def pricing_slope(self, request: dict, now: int) -> float:
        """The slope to price with: the dealer's slope flattened by what we do not know yet about the level
        (filter variance, price-war mixture spread, effect variance, outage uncertainty)."""
        self._roll(now)
        mus = [f.m + f.d + self._pw_off(k, now) for k, f in enumerate(self.filters)]
        mu = sum(w * m for w, m in zip(self.wts, mus))
        v = sum(w * (f.mean_var()[1] + (m - mu) ** 2) for w, f, m in zip(self.wts, self.filters, mus))
        v += self.effect(request)[1]
        if self.in_window(now):
            p = self.p_down(request["legs"][0]["pickup"]["placeId"])
            v += p * (1 - p) * self.outage_shift ** 2
        return self.slope / math.sqrt(1 + math.pi * self.slope ** 2 * v / 8)

    def p_price_war(self, now: Optional[int] = None) -> float:
        if now is not None:
            self._roll(now)
        return self.wts[1]

    def predict(self, request: dict, lr: float, now: int) -> float:
        """P(win) at our cheapest log ratio lr (net of lane and trust), mixing the hypotheses."""
        self._roll(now)
        off = self.adjust(request, now)
        ev = self.effect(request)[1]
        return sum(w * f.predict(off - lr + self._pw_off(k, now), self.slope, ev)
                   for k, (w, f) in enumerate(zip(self.wts, self.filters)))

    def observe(self, request: dict, lr: float, won: bool, now: Optional[int] = None) -> None:
        """One quote outcome. lr: our cheapest quoted log(price/list), net of lane offset and trust boost.
        now: the decision minute (respondBy by default); the competitor quoted at arrival."""
        t_dec = request["respondBy"] if now is None else now
        self._roll(t_dec)
        t_q = request["arrivedAt"]
        if t_q // DAY != self.day:
            t_q = t_dec
        place = request["legs"][0]["pickup"]["placeId"]
        inwin = self.in_window(t_q)
        off = self.p_down(place) * self.outage_shift if inwin else 0.0
        # Outage evidence: each region containing the pickup, "down" against "up", at today's level.
        if inwin and self.p_out_day > 0 and place in self.centers_of:
            em, ev = self.effect(request)
            x = em - lr
            p0 = sum(w * f.predict(x, self.slope, ev) for w, f in zip(self.wts, self.filters))
            p1 = sum(w * f.predict(x + self.outage_shift, self.slope, ev) for w, f in zip(self.wts, self.filters))
            p0, p1 = min(max(p0, 1e-4), 1 - 1e-4), min(max(p1, 1e-4), 1 - 1e-4)
            l = math.log(p1 / p0) if won else math.log((1 - p1) / (1 - p0))
            for c in self.centers_of[place]:
                self.llr[c] += l
        n_obs, warm_n = getattr(self, "n_obs", 0), getattr(self, "warm_n", 0)   # (old checkpoints lack these)
        if warm_n and n_obs < warm_n:
            q = self.warm_q ** 2 * (1 - n_obs / warm_n)
            for f in self.filters:
                f.pmm += q
        self.n_obs = n_obs
        # Price-war mixture weights, then each filter's update. The war runs 08:00-18:00 only.
        em, ev = self.effect(request)
        xs = [off + em - lr + self._pw_off(k, t_q) for k in range(2)]
        lik = [f.predict(x, self.slope, ev) for f, x in zip(self.filters, xs)]
        lik = [p if won else 1 - p for p in lik]
        ws = [w * max(l, 1e-6) for w, l in zip(self.wts, lik)]
        s = sum(ws)
        marginal = s                            # P(this outcome) before the update, for PriorMix
        self.wts = [w / s for w in ws] if s > 0 else self.wts
        if self.wts[1] < 1e-6:
            self.wts = [1.0 - 1e-6, 1e-6]
        gs = [f.update(x, won, self.slope, ev) for f, x in zip(self.filters, xs)]
        if self.eff_sd:
            g = sum(w * r[0] for w, r in zip(self.wts, gs))
            h = sum(w * r[1] for w, r in zip(self.wts, gs))
            v = sum(w * r[2] for w, r in zip(self.wts, gs))
            for k in self._keys(request):
                e = self.eff[k]
                e[0] += e[1] * g / (1 + h * (v + ev))
                e[1] -= e[1] * e[1] * h / (1 + h * (v + ev))
        self.n_today += 1
        self.n_obs += 1
        return marginal

    def _keys(self, request: dict) -> list:
        ks = []
        for kind, sd in self.eff_sd.items():
            if kind == "pickup":
                v = request["legs"][0]["pickup"]["placeId"]
            elif kind == "delivery":
                v = request["legs"][0]["delivery"]["placeId"]
            else:
                v = request.get("dealerId")
            ks.append((kind, v))
            if (kind, v) not in self.eff:
                self.eff[(kind, v)] = [0.0, sd * sd]
        return ks

    def effect(self, request: dict) -> tuple:
        """(mean, variance) of this request's learned pickup/delivery/dealer price offset."""
        if not self.eff_sd:
            return 0.0, 0.0
        ks = self._keys(request)
        return sum(self.eff[k][0] for k in ks), sum(self.eff[k][1] for k in ks)

    def _pw_off(self, k: int, t: int) -> float:
        return -self.pw_shift if k == 1 and not self.in_window(t) else 0.0

    def state(self) -> dict:
        pout, c = self.outage_posterior()
        return dict(day=self.day, level=round(math.exp(self.level(self.day * DAY + WIN_FROM)), 3),
                    p_pw=round(self.wts[1], 3), p_outage=round(pout, 3), center=c)


class PriorMix:
    """"Learn fast when the prior is wrong, settle when it is right": two Regime learners that differ only in the
    prior width of the world level (today's tight one and a wide one), weighted by Bayes on how well each predicted
    our wins and losses. In a typical week the tight learner keeps the weight and nothing changes; in a week far
    from the prior (the 14:00 test final: competitor ~1.2x list against a 1.37x prior) the wide one takes over
    within a few dozen quotes, and its day-one deviation goes into the world level instead of the day factor or
    the price-war hypothesis. Same interface as Regime for the dispatcher (level, adjust, observe)."""

    def __init__(self, members: list, weights: list, floor: float = 0.0):
        self.members, self.w, self.floor = members, list(weights), floor

    def level(self, now: int) -> float:
        return sum(w * m.level(now) for w, m in zip(self.w, self.members))

    def adjust(self, request: dict, now: int) -> float:
        return sum(w * m.adjust(request, now) for w, m in zip(self.w, self.members))

    def predict(self, request: dict, lr: float, now: int) -> float:
        return sum(w * m.predict(request, lr, now) for w, m in zip(self.w, self.members))

    def observe(self, request: dict, lr: float, won: bool, now: Optional[int] = None) -> float:
        lik = [m.observe(request, lr, won, now) for m in self.members]
        s = sum(w * l for w, l in zip(self.w, lik))
        ws = [w * l / s for w, l in zip(self.w, lik)] if s > 0 else self.w
        if self.floor:
            ws = [max(self.floor, w) for w in ws]
            ws = [w / sum(ws) for w in ws]
        self.w = ws
        return s

    def p_price_war(self, now: Optional[int] = None) -> float:
        return sum(w * m.p_price_war(now) for w, m in zip(self.w, self.members))

    def p_down(self, place: str) -> float:
        return sum(w * m.p_down(place) for w, m in zip(self.w, self.members))

    def _roll(self, now: int) -> None:
        for m in self.members:
            m._roll(now)

    @property
    def history(self) -> list:
        return self.members[max(range(len(self.w)), key=self.w.__getitem__)].history

    def state(self) -> dict:
        return {**self.members[max(range(len(self.w)), key=self.w.__getitem__)].state(), "w_wide": round(self.w[-1], 3)}


class SGD:
    """The dispatcher's current learner (dispatch.py, level 8), for comparison."""

    def __init__(self, comp0=1.37, slope=8.7, lr=0.02, lo=1.22, hi=1.60):
        self.lc, self.slope, self.eta, self.lo, self.hi = math.log(comp0), slope, lr, math.log(lo), math.log(hi)

    def level(self, now):
        return self.lc

    def adjust(self, request, now):
        return 0.0

    def pricing_slope(self, request, now):
        return self.slope

    def predict(self, request, lr, now):
        return _sig(self.slope * (self.lc - lr))

    def observe(self, request, lr, won, now=None):
        p = _sig(self.slope * (self.lc - lr))
        self.lc = min(self.hi, max(self.lo, self.lc + self.eta * ((1.0 if won else 0.0) - p)))
