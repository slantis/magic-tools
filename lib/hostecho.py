# -*- coding: utf-8 -*-
"""Fold the sub-elements Revit reports as hidden only because their container is.

Why this exists
---------------
`Element.IsHidden(view)` returns True for every sub-element of a hidden
container, not just for the container. So one "Hide in View > Element" on a
curtain wall makes every one of its panels, mullions AND grid lines answer
True. A tool that walks elements one by one and reports each True turns a
single click by the user into hundreds of rows.

Measured 2026-09-10 in a controlled case (a 40 ft `Storefront` created in an
empty project, hiding ONLY the wall, then a closed sweep of the whole
document): the flag lands on **68** elements, 1 wall + 16 curtain panels + 42
mullions + 8 curtain grid lines. Nothing else in the model moves. Same in a 3D
view and in a plan, so it does not depend on the view type. On a real
storefront the count was 169 children for one wall.

Why `GetDependentElements` and not `Host`
-----------------------------------------
The first version walked `Host` upwards. It missed the grid lines, which are
`CurtainGridLine` and have no `Host` property at all (it raises
AttributeError) -- reported from the field the same day. `GetDependentElements`
is the general relation and was measured to be exact on a real storefront: it
returned the 169 grid children and itself, with zero extras and zero misses.
It is also directional -- a panel, a mullion and a grid line do NOT list the
wall -- so a child can never claim its own container.

`SuperComponent` is not used: it is the nested-family relationship, not the
hosted-sub-element one, and it has no measured case behind it here.

What this module does, and what it deliberately does not
--------------------------------------------------------
It FOLDS, it does not discard. `IsHidden()` is a bare boolean with no
provenance: there is no way to tell "this panel looks hidden because its
container cascades" from "this panel carries its own hide AND its container
happens to be hidden too". Dropping the child would swallow the second case,
which is exactly the signal an override audit exists to surface. So the
child's id is kept on the container's row (see the `folded` map) and every
action that unhides still reaches it. Verified in Revit: `UnhideElements` over
container plus folded children brought all 59 back, none left hidden.
"""


def id_val(eid):
    # ElementId value compat Revit 2022-2026 (IntegerValue removed in 2026)
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


class DependentIndex(object):
    """Caches `GetDependentElements` per element for the length of a run.

    The relation is a model fact, not a per-view one, so an audit that walks
    many views must share ONE index: each element is then queried at most once
    for the whole scan instead of once per view.
    """

    def __init__(self):
        self._cache = {}

    def children_of(self, el):
        try:
            key = id_val(el.Id)
        except Exception:
            return set()
        got = self._cache.get(key)
        if got is None:
            try:
                got = set(id_val(i) for i in el.GetDependentElements(None))
            except Exception:
                got = set()
            got.discard(key)
            self._cache[key] = got
        return got


def fold_by_dependents(hidden_els, index=None):
    """Split hidden elements into the ones worth a row and the folded echoes.

    Returns `(survivors, folded)`. `survivors` keeps the input order and holds
    the elements no hidden container claims. `folded` maps a survivor's id
    value to the list of ElementId folded onto it.

    Containers are visited in ascending id order so the outermost one (created
    first, hence lower id) claims a shared sub-element before any inner one
    does, and the first claim wins.
    """
    if index is None:
        index = DependentIndex()

    by_id = {}
    for el in hidden_els:
        try:
            by_id[id_val(el.Id)] = el
        except Exception:
            pass
    hidden_ids = set(by_id)

    owner = {}
    for key in sorted(hidden_ids):
        for kid in index.children_of(by_id[key]) & hidden_ids:
            if kid != key and kid not in owner:
                owner[kid] = key

    alive = set(k for k in hidden_ids if k not in owner)

    folded = {}
    for kid, own in owner.items():
        # The claimed child may hang off a container that was itself claimed
        # (a grid line owns panels, and the wall owns the grid line). Walk up
        # to the nearest surviving ancestor so nothing is lost.
        target = own
        walked = set()
        while target not in alive:
            if target in walked:
                break
            walked.add(target)
            nxt = owner.get(target)
            if nxt is None or nxt == target:
                break
            target = nxt
        if target in alive:
            folded.setdefault(target, []).append(by_id[kid].Id)
        else:
            # Nowhere to fold it onto: keep its own row rather than drop it.
            alive.add(kid)

    survivors = [el for el in hidden_els if id_val(el.Id) in alive]
    return survivors, folded


def suffix(n):
    """The label a container row carries for the sub-elements folded into it."""
    if not n:
        return u''
    return u' (+{} nested)'.format(n)
