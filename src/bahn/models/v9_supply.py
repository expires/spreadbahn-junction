"""Fitted driver-supply, booking-hazard and lateness models for Spread level-8 dispatch v8.

Loads coefficients from data/fits/supply.json (fitted on the 228-world level-8 archive).
Everything here is deterministic, pure Python + math, and cheap enough to call a few thousand
times per simulated week.

Public API (what dispatch v8 imports)
-------------------------------------
    from supply import (load_models, build_supply, order_features, gig_features,
                        supply_for_places, supply_total, weather_exposed)

    models   = load_models()                     # cached; reads data/fits/supply.json
    supply   = build_supply(roster, road)        # ONCE per world: dict[placeId] -> dict
    feat     = order_features(request, option, road, supply, outstanding=0)   # quote-time
    p_late   = models.lateness.p_late(feat)      # P(order late) in [0,1]
    exp_fee  = models.booking.expected_fee_cents(feat)   # expected driver fee, CENTS

    gfeat    = gig_features(supply=supply, supply_pickup=..., supply_delivery=...,
                            km=..., route_min=..., lead_h=..., n_cars=1, n_orders=1,
                            ref_cents=<sum of the gig's reference fees in cents>,
                            now=<world minute>, ...)
    p_book1h = models.booking.p_book(gfeat, schedule=None, horizon_h=1.0)
    p_book8h = models.booking.p_book(gfeat, schedule=[{'at': now, 'feeCents': ...}], horizon_h=8.0)

'road' in build_supply/order_features is either dispatch.Road (has .minutes(a,b) / .km(a,b))
or the raw {'places': ..., 'roads': [...]} map dict from GET /v1/map.

Model notes / identifiability
-----------------------------
* The fee elasticity in the booking hazard is fitted on 2,357 gigs whose fee ratio was assigned
  at random in six exploration worlds (ratio uniform over ~0.7-2.2 x the reference fee), so it is
  not confounded by the posting policy the other worlds used.
* The rest of the hazard (supply, lead time, km, hour, weekday, cars/bundles, weather) is fitted
  on all 84,978 archived gigs / 492,937 hourly bin rows, with the fee effect as a fixed offset.
* The lateness model is a plain logistic on 79,625 won orders; features are all observable at
  quote time. It has no fee information, so it answers "how risky is this order given supply,
  slack and our backlog", not "how risky if we pay more".
* All archive worlds were played by earlier policies; orders that those policies declined are
  not observed. Predictions are therefore conditional on "an order we chose to win".
"""
from __future__ import annotations

import json
import math
import os
from collections import Counter
from functools import lru_cache

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FIT_PATH = os.path.join(ROOT, "data", "fits", "supply.json")
HOUR, DAY = 60, 24 * 60
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ----------------------------------------------------------------------------------------------
# roads / map
# ----------------------------------------------------------------------------------------------


class _Road:
    """Uniform access to the map: accepts dispatch.Road-like objects or the raw /v1/map dict."""

    def __init__(self, src):
        if hasattr(src, "minutes") and hasattr(src, "km"):
            self.minutes, self.km = src.minutes, src.km
            self.places = None
        elif isinstance(src, dict) and "roads" in src:
            self._m = {(r["from"], r["to"]): r for r in src["roads"]}
            self.places = [p["id"] for p in src.get("places", [])]
            self.minutes = lambda a, b: int(round(self._m[(a, b)]["minutes"]))
            self.km = lambda a, b: float(self._m[(a, b)]["km"])
        else:  # pragma: no cover
            raise TypeError("road must be a dispatch.Road or the /v1/map dict")
        if self.places is None:
            pairs = set()
            try:
                pairs = set(getattr(src, "r", {}).keys()) if hasattr(src, "r") else set()
            except Exception:
                pairs = set()
            self.places = sorted({a for a, _ in pairs} | {b for _, b in pairs})


def load_map(path=None):
    """The world map (all worlds share it). Used only by the fitting code / stand-alone use."""
    for cand in (path, os.path.join(ROOT, "data", "fits", "map.json"), os.path.join(ROOT, "data", "map.json")):
        if cand and os.path.exists(cand):
            with open(cand) as fh:
                return json.load(fh)
    raise FileNotFoundError("no map at data/fits/map.json or data/map.json")


# ----------------------------------------------------------------------------------------------
# per-place supply
# ----------------------------------------------------------------------------------------------


def build_supply(roster, road, decay_minutes=60.0):
    """Per-place driver supply from the driver roster of the current world.

    'roster' is the list from GET /v1/drivers (fields homePlaceId, rating, licence,
    completedGigs). Returns {placeId: dict} with keys

        drivers          int, drivers whose home is that place
        kernel           float, sum over home towns h of n_h * exp(-road_minutes(h, p)/decay)
        manual           float, same kernel but only manual-licence drivers
        experienced      float, same kernel but only rating>=4.8 and completedGigs>=50
        nearest_min      int, road minutes to the nearest town with any driver
        nearest_drivers  int, drivers living in that nearest town

    The 'kernel' is the number dispatch.py calls supply[p]; it is the expected count of drivers
    reachable at about one decay-time of travel.
    """
    rd = _Road(road)
    homes = Counter(d.get("homePlaceId") for d in roster)
    manual = Counter(d.get("homePlaceId") for d in roster if d.get("licence") == "manual")
    experienced = Counter(d.get("homePlaceId") for d in roster
                          if float(d.get("rating", 0)) >= 4.8 and int(d.get("completedGigs", 0)) >= 50)
    places = rd.places or sorted(homes)
    out = {}
    for p in places:
        k = sum(n * math.exp(-rd.minutes(h, p) / decay_minutes) for h, n in homes.items() if n)
        km = sum(n * math.exp(-rd.minutes(h, p) / decay_minutes) for h, n in manual.items() if n)
        ke = sum(n * math.exp(-rd.minutes(h, p) / decay_minutes) for h, n in experienced.items() if n)
        if homes:
            best = min(homes, key=lambda h: rd.minutes(h, p))
            nearest_min, nearest_n = rd.minutes(best, p), homes[best]
        else:
            nearest_min, nearest_n = 0, 0
        out[p] = {"drivers": int(homes.get(p, 0)), "kernel": float(k), "manual": float(km),
                  "experienced": float(ke), "nearest_min": int(nearest_min), "nearest_drivers": int(nearest_n)}
    return out


def supply_total(supply):
    """Total number of drivers in the roster (world size); used as log_N feature."""
    return sum(v.get("drivers", 0) for v in supply.values())


def _supply_kernel(supply, place):
    v = supply.get(place)
    if v is None:
        return 1.0
    return v.get("kernel", v) if isinstance(v, dict) else float(v)


def supply_for_places(supply, pickup_places, delivery_places):
    """Supply features for a request/gig from its pickup and delivery place ids.

    Returns dict with supply_pickup (first pickup), supply_delivery (first delivery),
    supply_min / supply_max over all listed places.
    """
    pk = list(pickup_places) or [None]
    dl = list(delivery_places) or [None]
    sp = _supply_kernel(supply, pk[0])
    sd = _supply_kernel(supply, dl[0])
    vals = [_supply_kernel(supply, p) for p in list(pk) + list(dl) if p is not None] or [1.0]
    return {"supply_pickup": float(sp), "supply_delivery": float(sd),
            "supply_min": float(min(vals)), "supply_max": float(max(vals))}


# ----------------------------------------------------------------------------------------------
# weather
# ----------------------------------------------------------------------------------------------


def weather_exposed(request, forecasts):
    """1 when a forecast event puts snow at a place of the request during its window; else 0.
    'forecasts' is the list of forecast events seen so far (dicts with from/to/placeIds/at)."""
    if not forecasts:
        return 0
    places = set()
    for leg in request.get("legs", []):
        places.add(leg["pickup"]["placeId"])
        places.add(leg["delivery"]["placeId"])
    deadline = min([o["deadline"] for o in request.get("options", [])] or [10 ** 9])
    for f in forecasts:
        if f.get("at", 0) > request.get("respondBy", 0):
            continue
        if places & set(f.get("placeIds", [])):
            if f.get("from", 0) < deadline and f.get("to", 0) > request.get("respondBy", 0):
                return 1
    return 0


# ----------------------------------------------------------------------------------------------
# feature builders
# ----------------------------------------------------------------------------------------------


def _request_leg_supply(request, road, supply):
    rd = _Road(road)
    picks, dels, km, drive = [], [], 0.0, 0
    private = premium = electric = 0
    for leg in request.get("legs", []):
        p, d = leg["pickup"]["placeId"], leg["delivery"]["placeId"]
        picks.append(p); dels.append(d)
        km += rd.km(p, d); drive += rd.minutes(p, d)
        private = private or int(bool(leg["pickup"].get("privateAddress") or leg["delivery"].get("privateAddress")))
        premium = premium or int(bool(leg["car"].get("premium")))
        electric = electric or int(bool(leg["car"].get("electric")))
    f = supply_for_places(supply, picks, dels)
    f.update({"km": float(km), "drive_min": int(drive), "n_legs": len(request.get("legs", [])),
              "private": private, "premium": premium, "electric": electric})
    return f


def order_features(request, option, road, supply, outstanding=0, weather=0, explore=0,
                   n_orders2=0, n_cars_gig=1):
    """Quote-time feature dict for LatenessModel.p_late (and the fee model).

    Parameters
    ----------
    request : PriceRequest as from the event stream / snapshot.
    option  : one DeliveryOption dict of that request (kind + deadline + listPriceCents).
    road    : dispatch.Road or /v1/map dict.
    supply  : dict from build_supply.
    outstanding : how many of our already-won orders are still undelivered (backlog pressure).
    weather : 1 if snow is forecast over this request's window (see weather_exposed).
    explore : legacy flag (0 for the production dispatcher).
    n_orders2 / n_cars_gig : set for orders won through a multi-order gig (0/1 and 1/2).

    The returned dict is directly usable by LatenessModel.p_late; it also carries a few raw
    fields (slack_h, ref_fee_cents, supply_pickup...) for the caller.
    """
    base = _request_leg_supply(request, road, supply)
    total = max(1, supply_total(supply))
    slack_h = (option["deadline"] - base["drive_min"] - 30 - request["respondBy"]) / 60.0
    slack_c = max(-24.0, min(240.0, slack_h))
    ready_after_q_h = 0.0
    try:
        ready_after_q_h = max(0.0, (max(l["pickup"]["window"]["from"] for l in request["legs"]) - request["respondBy"]) / 60.0)
    except Exception:
        pass
    at = request.get("arrivedAt", request.get("respondBy", 0))
    day = at // DAY
    kind = option.get("kind")
    return {
        "log_S_pick": math.log1p(base["supply_pickup"]),
        "log_S_del": math.log1p(base["supply_delivery"]),
        "log_S_max": math.log1p(base["supply_max"]),
        "log_N": math.log(total),
        "km100": base["km"] / 100.0,
        "drive_h": base["drive_min"] / 60.0,
        "slack_pos": math.log1p(max(0.0, slack_c)),
        "slack_neg": float(slack_c < 0),
        "slack_inv": 1.0 / (1.0 + max(0.0, slack_c)),
        "n_legs": float(base["n_legs"]),
        "private": float(base["private"]),
        "premium": float(base["premium"]),
        "electric": float(base["electric"]),
        "note": float(bool(request.get("note"))),
        "ready_after_q_h": min(240.0, ready_after_q_h),
        "weather": float(weather),
        "opt_same": float(kind == "same_day"),
        "opt_next": float(kind == "next_day"),
        "hour_sin": math.sin(2 * math.pi * ((at // HOUR) % 24) / 24.0),
        "hour_cos": math.cos(2 * math.pi * ((at // HOUR) % 24) / 24.0),
        "day": day / 4.0,
        "explore": float(explore),
        "outstanding": math.log1p(max(0.0, min(5000.0, float(outstanding)))),
        "n_orders2": float(n_orders2),
        "n_cars_gig": float(n_cars_gig),
        "slack_h": slack_h, "ref_fee_cents": int(request.get("referenceFeeCents", 0)),
        "supply_pickup": base["supply_pickup"], "supply_delivery": base["supply_delivery"],
    }


def gig_features(supply, supply_pickup, supply_delivery, km, route_min, lead_h,
                 now, n_cars=1, n_orders=1, chain_min=0.0, ref_cents=None, fee_ratio=0.9,
                 private=0, premium=0, aud_exp=0, aud_man=0, trade_in=0, snow=0,
                 supply_min=None, supply_max=None, N=None, hour=None, dow=None):
    """Feature dict for BookingModel.p_book / expected_fee_cents.

    Parameters
    ----------
    supply            : dict from build_supply (used if supply_min/max/N not given).
    supply_pickup     : kernel at the (first) pickup place.
    supply_delivery   : kernel at the (last) delivery place.
    km, route_min     : route distance and minutes between all stops in order, from the first
                        pickup to the last delivery. For a 2-car bundle include the drive from
                        the first car's delivery to the second car's pickup.
    lead_h            : hours from 'now' until the first pickup window opens (can be negative).
    now               : world minute when the gig is posted.
    n_cars, n_orders  : cars in the gig and distinct orders in the gig (1 for a single order).
    chain_min         : minutes of driving between consecutive cars (delivery->next pickup).
    ref_cents         : sum over the gig's cars of referenceFeeCents/n_legs(order), in cents.
                        This is the denominator of the fee ratio, so a 2-order bundle uses
                        refA+refB, i.e. the app fee is feeCents/ref_cents.
    fee_ratio         : opening fee / ref_cents (default 0.9).
    """
    total = float(N) if N is not None else max(1.0, float(supply_total(supply)))
    sp = float(supply_pickup); sd = float(supply_delivery)
    smin = float(supply_min) if supply_min is not None else min(sp, sd)
    smax = float(supply_max) if supply_max is not None else max(sp, sd)
    t = int(now)
    return {
        "now": t,
        "lead_h": float(lead_h),
        "supply_pickup": sp, "supply_delivery": sd, "supply_min": smin, "supply_max": smax,
        "log_S_pick": math.log1p(sp), "log_S_del": math.log1p(sd),
        "log_S_min": math.log1p(smin), "log_S_max": math.log1p(smax),
        "log_N": math.log(total),
        "km100": float(km) / 100.0, "route_h": float(route_min) / 60.0, "route_km100": float(km) / 100.0,
        "chain_min100": float(chain_min) / 100.0,
        "n_cars": float(n_cars), "n_orders": float(n_orders), "n_orders2": float(n_orders > 1),
        "trade_in": float(trade_in), "private": float(private), "premium": float(premium),
        "aud_exp": float(aud_exp), "aud_man": float(aud_man), "snow_any": float(snow),
        "explore": 0.0,
        "hour": (int(hour) if hour is not None else (t // HOUR) % 24),
        "dow": (int(dow) if dow is not None else (t // DAY) % 7),
        "ref_cents": int(ref_cents if ref_cents is not None else 0),
        "fee_ratio": float(fee_ratio),
    }


# ----------------------------------------------------------------------------------------------
# models
# ----------------------------------------------------------------------------------------------


def _sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


class _LinearModel:
    def __init__(self, blob):
        self.features = list(blob["features"])
        self.coef = [float(c) for c in blob["coef"]]
        if "intercept" in blob:
            self.intercept = float(blob["intercept"])
        else:  # some fits store the intercept as coef[0] (features excludes the constant)
            self.intercept = self.coef[0]
            self.coef = self.coef[1:]
        self.defaults = {k: float(v) for k, v in blob.get("defaults", {}).items()}
        self._idx = {n: i for i, n in enumerate(self.features)}

    def vec(self, feats):
        d = self.defaults
        return [float(feats.get(n, d.get(n, 0.0))) for n in self.features]

    def logit(self, feats):
        return self.intercept + sum(c * float(feats.get(n, self.defaults.get(n, 0.0)))
                                    for c, n in zip(self.coef, self.features))

    def prob(self, feats):
        return _sigmoid(self.logit(feats))


class LatenessModel(_LinearModel):
    """P(order is late) from quote-time features. ~0.786 grouped-CV AUC on 228 archive worlds."""

    def p_late(self, feats):
        return self.prob(feats)

    def p_late_option(self, request, option, road, supply, **kw):
        """Convenience: compute features then P(late) for one delivery option."""
        return self.p_late(order_features(request, option, road, supply, **kw))


class DriverFeeModel(_LinearModel):
    """Expected driver fee actually paid for an order (ledger driver_fee lines).

    Target is log1p(paid / referenceFee); expected_fee_cents returns cents.
    """

    def _fee_feats(self, feats):
        """Map quote-time or gig-time feature dicts onto the fee model's feature names."""
        f = dict(feats)
        fr = feats.get("first_ratio", feats.get("fee_ratio", 0.9))
        f.setdefault("first_ratio", fr)
        f.setdefault("first_ratio2", fr * fr)
        if "ref_cents" in feats:
            f.setdefault("ref_fee_cents", feats["ref_cents"])
        n_orders = feats.get("n_orders", 1)
        f.setdefault("n_orders2", 1.0 if float(n_orders) > 1 else 0.0)
        if "route_min" in feats:
            f.setdefault("route_h", float(feats["route_min"]) / 60.0)
        if "km" in feats:
            f.setdefault("km100", float(feats["km"]) / 100.0)
        if "chain_min" in feats:
            f.setdefault("chain_min100", float(feats["chain_min"]) / 100.0)
        if "hour" in feats:
            f.setdefault("hour_sin", math.sin(2 * math.pi * (float(feats["hour"]) % 24) / 24.0))
            f.setdefault("hour_cos", math.cos(2 * math.pi * (float(feats["hour"]) % 24) / 24.0))
        f.setdefault("slack_h", feats.get("slack_h", 24.0))
        f.setdefault("n_posts2", 0.0)
        f.setdefault("explore", 0.0)
        return f

    def expected_paid_ratio(self, feats):
        return max(0.0, math.expm1(self.logit(self._fee_feats(feats))))

    def expected_fee_cents(self, feats):
        ref = float(feats.get("ref_fee_cents", feats.get("ref_cents", 0)))
        if ref <= 0:
            ref = 6000.0
        return self.expected_paid_ratio(feats) * ref


class BookingModel(_LinearModel):
    """Discrete-time booking hazard: P(a posted gig gets a driver within a horizon).

    The hazard is fitted per 1-hour bin with the fee effect as a fixed offset (estimated on
    randomised-fee gigs) plus risk features (supply, lead time, km, route, bundle/cars,
    audience, weather) and an age baseline.
    """

    FEE_ANCHOR = 0.9
    FEE_HINGE = 1.5

    def __init__(self, blob, fee_blob=None):
        super().__init__(blob)
        fb = fee_blob or {"a": 0.0, "b": 0.0}
        self.fee_a = float(fb.get("a", 0.0))
        self.fee_b = float(fb.get("b", 0.0))
        self.fee_model = None

    def fee_offset(self, ratio):
        r = min(20.0, max(0.05, float(ratio)))
        return self.fee_a * math.log(r / self.FEE_ANCHOR) + self.fee_b * max(0.0, math.log(r / self.FEE_HINGE))

    def hazard(self, feats, k, ratio):
        base = self.intercept + self.fee_offset(ratio)
        lead = max(-48.0, min(168.0, float(feats.get("lead_h", 2.0)) - k))
        t = int(feats.get("now", 0)) + k * HOUR
        hr, dw = (t // HOUR) % 24, (t // DAY) % 7
        dyn = {"lead_clip": lead, "lead_neg": 1.0 if lead < 0 else 0.0, "lead_clip2": lead * lead / 100.0,
               "age_1": 1.0 if k == 1 else 0.0, "age_2": 1.0 if k == 2 else 0.0,
               "age_3": 1.0 if k == 3 else 0.0, "age_4": 1.0 if k == 4 else 0.0,
               "age_5": 1.0 if k == 5 else 0.0, "age_6p": 1.0 if k >= 6 else 0.0}
        for h in range(1, 24):
            dyn["hr_%d" % h] = 1.0 if hr == h else 0.0
        for d in range(1, 7):
            dyn["dw_%d" % d] = 1.0 if dw == d else 0.0
        s = base
        for c, n in zip(self.coef, self.features):
            if n in dyn:
                s += c * dyn[n]
            else:
                s += c * float(feats.get(n, self.defaults.get(n, 0.0)))
        return _sigmoid(s)

    @staticmethod
    def _fee_at(schedule, t):
        if not schedule:
            return None
        fee = schedule[0]["feeCents"]
        for s in schedule:
            if s["at"] <= t:
                fee = s["feeCents"]
            else:
                break
        return float(fee)

    def p_book(self, feats, schedule=None, horizon_h=24.0, step_minutes=60):
        """P(a gig posted now gets its first driver before 'horizon_h' hours).

        'schedule' is a list of {'at': worldMinute, 'feeCents': int} (the posted fee schedule,
        non-falling). The fee ratio shown in the app at time t is fee(t)/ref_cents.
        With schedule=None the flat feats['fee_ratio'] (default 0.9) is used.
        """
        ref = float(feats.get("ref_cents", 0))
        now = int(feats.get("now", 0))
        flat = float(feats.get("fee_ratio", 0.9))
        steps = max(1, int(round(horizon_h * 60.0 / step_minutes)))
        surv = 1.0
        for k in range(steps):
            fee = self._fee_at(schedule, now + k * step_minutes)
            ratio = (fee / ref) if (fee is not None and ref > 0) else flat
            h = min(0.999, max(0.0, self.hazard(feats, k, ratio)))
            if step_minutes != 60:
                h = 1.0 - (1.0 - h) ** (step_minutes / 60.0)
            surv *= (1.0 - h)
            if surv < 1e-9:
                break
        return 1.0 - surv

    def p_book_curve(self, feats, schedule=None, horizons=(1, 3, 8, 24)):
        """P(booked) at several horizons in one simulation pass."""
        ref = float(feats.get("ref_cents", 0))
        now = int(feats.get("now", 0))
        flat = float(feats.get("fee_ratio", 0.9))
        hs = sorted(set(int(x) for x in horizons))
        out, surv = {}, 1.0
        for k in range(max(hs)):
            fee = self._fee_at(schedule, now + k * HOUR)
            ratio = (fee / ref) if (fee is not None and ref > 0) else flat
            surv *= (1.0 - min(0.999, max(0.0, self.hazard(feats, k, ratio))))
            if (k + 1) in hs:
                out[k + 1] = 1.0 - surv
        return out

    def fee_for_target(self, feats, target=0.9, horizon_h=8.0, ref_lo=0.4, ref_hi=3.0):
        """Smallest flat fee ratio (fee / sum reference fees) that reaches 'target' booking
        probability within 'horizon_h' hours. Returns None when unreachable in [lo, hi]."""
        lo, hi = float(ref_lo), float(ref_hi)
        f = dict(feats); f["fee_ratio"] = hi
        if self.p_book(f, None, horizon_h) < target:
            return None
        f["fee_ratio"] = lo
        if self.p_book(f, None, horizon_h) >= target:
            return lo
        for _ in range(18):
            mid = 0.5 * (lo + hi)
            f["fee_ratio"] = mid
            if self.p_book(f, None, horizon_h) >= target:
                hi = mid
            else:
                lo = mid
        return hi

    def expected_fee_cents(self, feats):
        if self.fee_model is None:
            return float(feats.get("ref_cents", feats.get("ref_fee_cents", 6000))) * 0.9
        return self.fee_model.expected_fee_cents(feats)


class SupplyModels:
    def __init__(self, path=DEFAULT_FIT_PATH):
        with open(path) as fh:
            blob = json.load(fh)
        self.meta = blob
        self.booking = BookingModel(blob["booking"], blob["booking"].get("fee_offset"))
        self.lateness = LatenessModel(blob["lateness"])
        self.driver_fee = DriverFeeModel(blob["driver_fee"])
        self.booking.fee_model = self.driver_fee

    def __repr__(self):
        return "SupplyModels(trained=%r)" % (self.meta.get("trained"),)


@lru_cache(maxsize=4)
def load_models(path=DEFAULT_FIT_PATH):
    """Load (and cache) the fitted models from data/fits/supply.json."""
    return SupplyModels(path)


def p_late(feats, models=None):
    return (models or load_models()).lateness.p_late(feats)


def p_book(feats, schedule=None, horizon_h=24.0, models=None):
    return (models or load_models()).booking.p_book(feats, schedule, horizon_h)


if __name__ == "__main__":  # tiny smoke test
    m = load_models()
    print(m)
    road = _Road(load_map())
    roster = [{"id": str(i), "homePlaceId": "helsinki" if i % 3 else "tampere", "rating": 4.9,
               "licence": "manual", "completedGigs": 80} for i in range(600)]
    sup = build_supply(roster, road)
    print("supply helsinki", sup["helsinki"])
    req = {"id": "R1", "dealerId": "D1", "arrivedAt": 1000, "respondBy": 1015,
           "legs": [{"car": {"premium": False, "electric": False, "chargePercent": None},
                     "pickup": {"placeId": "helsinki", "privateAddress": False, "window": {"from": 1200, "to": 1800}, "hours": None},
                     "delivery": {"placeId": "tampere", "privateAddress": False, "window": {"from": 1200, "to": 1800}, "hours": None}}],
           "options": [{"kind": "same_day", "deadline": 1800, "listPriceCents": 5000},
                       {"kind": "next_day", "deadline": 3240, "listPriceCents": 4600}],
           "referenceFeeCents": 6000, "note": None}
    f = order_features(req, req["options"][0], road, sup)
    print("p_late same_day", round(m.lateness.p_late(f), 4))
    print("p_late next_day", round(m.lateness.p_late(order_features(req, req["options"][1], road, sup)), 4))
    g = gig_features(sup, sup["helsinki"]["kernel"], sup["tampere"]["kernel"], road.km("helsinki", "tampere"),
                     road.minutes("helsinki", "tampere"), lead_h=3.0, now=1015, ref_cents=6000)
    print("p_book 1h @0.9", round(m.booking.p_book(g, None, 1), 4), " 8h", round(m.booking.p_book(g, None, 8), 4))
    print("fee for 90% in 8h", round(m.booking.fee_for_target(g, 0.9, 8) or -1, 3))
    print("expected fee cents", round(m.booking.expected_fee_cents(g), 1))
