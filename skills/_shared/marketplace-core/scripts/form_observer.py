#!/usr/bin/env python3
"""Provider-neutral observer for a marketplace's listing-creation form.

A day was spent discovering, one failed run at a time, what a single marketplace's listing
form actually is -- mechanically, not by judgement. A catalogue named a category the form's
select did not offer, and nothing caught it because the catalogue was self-consistent and only
wrong about the world. A subcategory select turned out to populate only after another select was
chosen, so it carried nothing useful up front. The form itself was a multi-step wizard with every
step already sitting in the DOM at once, non-current steps wrapped in an element whose CSS-module
class hid them -- a flat field-by-field fill produced a locator that resolved but whose bounding
box was zero-sized, which reads exactly like a stalled page unless something says otherwise.

None of that needed a judgement call. Reading option labels, telling a wizard from a single page,
noticing a select with nothing but a placeholder -- all of it is mechanical observation of markup
that was already sitting there. This module does only that: it reports what is on the page and
what evidence it saw. It decides nothing about what to sell, what a category means, or which
field to fill first -- that judgement belongs to whatever calls it, one layer up.

Two entry points share one internal walk of the parsed markup:

  * ``observe_html(html)``   -- pure, no browser; everything derivable from static markup.
  * ``observe_page(page)``   -- takes a live Playwright page and additionally reports what only a
                                 live DOM knows: computed visibility, zero-sized boxes, and which
                                 ancestor is hiding a field.

Both return the same report shape. Fields that need a live page (``visible``, ``bounding_box``,
``hiding_ancestor``) are simply absent from ``observe_html``'s result rather than set to a faked
value -- an unmeasured fact must never look like a negative one.

Parsing uses only ``html.parser`` from the standard library: this module already needs no other
dependency, and a second one would outlive whatever reason it was added for.

No line below names a specific marketplace. A test in this package's ``tests/`` directory asserts
that, the same way ``storefront_kernel.py``'s own tests assert it of that module.
"""

from __future__ import annotations

import difflib
from html.parser import HTMLParser
from typing import Any, Mapping, Sequence

__all__ = [
    "observe_html",
    "observe_page",
    "diff_vocabulary",
]

_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "legend"}
_BUTTON_TYPES = {"submit", "button"}
# Both scripts (Japanese, since the first marketplaces this runs against are Japanese) and
# English idioms a wizard's "move forward" control tends to use, checked in this priority
# order -- "next"-shaped words before a generic "confirm"/"submit" word, so a wizard's actual
# step-advance button is preferred over its final submit button when both are present.
_ADVANCE_KEYWORDS = (
    "次へ", "次のステップ", "つぎへ",
    "next", "continue", "proceed",
    "確認画面へ", "確認へ", "confirm", "確認",
)


# ---------------------------------------------------------------------------------
# A minimal DOM tree built on stdlib html.parser -- no external HTML library.
# ---------------------------------------------------------------------------------

class _Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text_parts", "sibling_index")

    def __init__(self, tag: str, attrs: dict[str, str | None], parent: "_Node | None"):
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node] = []
        self.parent = parent
        self.text_parts: list[str] = []
        self.sibling_index = 0

    @property
    def classes(self) -> list[str]:
        value = self.attrs.get("class") or ""
        return value.split()

    @property
    def text(self) -> str:
        return "".join(self.text_parts).strip()

    def iter_descendants(self):
        for child in self.children:
            yield child
            yield from child.iter_descendants()

    def css_path(self) -> str:
        """A positional selector from the document root, used only by ``observe_page`` to
        locate this exact node live -- never reported in the observation output itself."""
        parts = []
        node: _Node | None = self
        while node is not None and node.tag != "#root":
            parts.append(f"{node.tag}:nth-of-type({node.sibling_index + 1})")
            node = node.parent
        return " > ".join(reversed(parts))


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.stack: list[_Node] = [self.root]

    def _append(self, tag: str, attrs: list[tuple[str, str | None]], push: bool) -> None:
        attrs_dict: dict[str, str | None] = dict(attrs)
        parent = self.stack[-1]
        sibling_index = sum(1 for child in parent.children if child.tag == tag)
        node = _Node(tag, attrs_dict, parent)
        node.sibling_index = sibling_index
        parent.children.append(node)
        if push and tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._append(tag, attrs, push=True)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._append(tag, attrs, push=False)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return
        # No open element of this name (malformed or over-closed markup): ignore rather than
        # raise, matching this house's other hand-rolled HTMLParser subclasses.

    def handle_data(self, data: str) -> None:
        self.stack[-1].text_parts.append(data)


def _parse_tree(html: str) -> _Node:
    parser = _TreeBuilder()
    parser.feed(html)
    parser.close()
    return parser.root


# ---------------------------------------------------------------------------------
# Field extraction.
# ---------------------------------------------------------------------------------

def _select_options(node: _Node) -> tuple[list[dict], int | None]:
    option_nodes = [child for child in node.children if child.tag == "option"]
    options = [{"value": opt.attrs.get("value"), "label": opt.text} for opt in option_nodes]
    # The placeholder convention on every marketplace this has been checked against: an
    # empty or absent value on the first option. A select with real choices in that first
    # slot (value present and non-empty) has no placeholder to identify.
    placeholder_index = 0 if options and (options[0]["value"] in (None, "")) else None
    for index, option in enumerate(options):
        option["is_placeholder"] = index == placeholder_index
    return options, placeholder_index


def _is_required(node: _Node) -> bool:
    if "required" in node.attrs:
        return True
    return (node.attrs.get("aria-required") or "").strip().lower() == "true"


def _identity(node: _Node) -> tuple[str | None, str]:
    name = node.attrs.get("name")
    if name:
        return name, "name"
    element_id = node.attrs.get("id")
    if element_id:
        return element_id, "id"
    return None, "none"


def _build_field(node: _Node) -> dict:
    identifier, source = _identity(node)
    field: dict[str, Any] = {
        "identifier": identifier,
        "identifier_source": source,
        "tag": node.tag,
        "required": _is_required(node),
    }
    if node.tag == "input":
        field["type"] = node.attrs.get("type")
    if node.tag == "select":
        options, placeholder_index = _select_options(node)
        field["options"] = options
        field["option_labels"] = [option["label"] for option in options]
        field["placeholder_index"] = placeholder_index
    return field


# ---------------------------------------------------------------------------------
# Wizard detection: a repeated wrapper class that hides all but one field-bearing group.
#
# Never hardcode the observed class string. The signature this looks for is purely positional:
# among a set of sibling elements that each wrap at least one form field, a class token present
# on exactly (n - 1) of them is what the build's CSS-module hash for "hidden" happens to look
# like -- but the detection itself never reads that word or that hash, only the count.
# ---------------------------------------------------------------------------------

def _contains_field(node: _Node) -> bool:
    if node.tag in {"input", "textarea", "select"}:
        return True
    return any(_contains_field(child) for child in node.children)


def _hiding_class_signature(children: list[_Node]) -> dict | None:
    count = len(children)
    if count < 2:
        return None
    token_counts: dict[str, int] = {}
    for child in children:
        for token in set(child.classes):
            token_counts[token] = token_counts.get(token, 0) + 1
    candidates = sorted(token for token, seen in token_counts.items() if seen == count - 1)
    if not candidates:
        return None
    return {"class": candidates[0], "candidates": candidates, "children_with_class": count - 1}


def _step_name(child: _Node, index: int) -> str:
    for node in [child, *child.iter_descendants()]:
        if node.tag in _HEADING_TAGS and node.text:
            return node.text
        if "title" in " ".join(node.classes).lower() and node.text:
            return node.text
    return f"Step {index + 1}"


def _is_descendant_or_self(ancestor: _Node, node: _Node) -> bool:
    current: _Node | None = node
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


def _detect_wizard(root: _Node) -> tuple[dict, list[_Node], _Node] | None:
    best: tuple[dict, list[_Node], _Node] | None = None
    for parent in [root, *root.iter_descendants()]:
        field_bearing = [child for child in parent.children if _contains_field(child)]
        if len(field_bearing) < 2:
            continue
        signature = _hiding_class_signature(field_bearing)
        if signature is None:
            continue
        if best is None or len(field_bearing) > len(best[1]):
            best = (signature, field_bearing, parent)
    return best


def _build_steps_report(
    wizard: tuple[dict, list[_Node], _Node] | None,
    field_nodes: list[_Node],
    fields: list[dict],
) -> dict:
    if wizard is None:
        return {"is_wizard": False, "wrapper_class": None, "detected_via": None, "steps": []}
    signature, children, parent = wizard
    identifier_by_node = {id(node): field["identifier"] for node, field in zip(field_nodes, fields)}
    steps = []
    for index, child in enumerate(children):
        step_field_ids = [
            identifier_by_node[id(node)] for node in field_nodes
            if _is_descendant_or_self(child, node)
        ]
        steps.append({
            "index": index,
            "name": _step_name(child, index),
            "has_wrapper_class": signature["class"] in child.classes,
            "fields": step_field_ids,
        })
    return {
        "is_wizard": True,
        "wrapper_class": signature["class"],
        "detected_via": {
            "parent_tag": parent.tag,
            "parent_classes": parent.classes,
            "sibling_count": len(children),
            "children_with_wrapper_class": signature["children_with_class"],
            "other_candidate_classes": [c for c in signature["candidates"] if c != signature["class"]],
        },
        "steps": steps,
    }


# ---------------------------------------------------------------------------------
# Advance control: discovered by text, every candidate reported alongside the chosen one.
# ---------------------------------------------------------------------------------

def _button_text(node: _Node) -> str:
    if node.tag == "input":
        return (node.attrs.get("value") or "").strip()
    return node.text


def _is_button_like(node: _Node) -> bool:
    if node.tag == "button":
        return True
    if node.tag == "input" and (node.attrs.get("type") or "").lower() in _BUTTON_TYPES:
        return True
    return (node.attrs.get("role") or "").lower() == "button"


def _detect_advance_control(root: _Node) -> dict:
    candidates = []
    for node in root.iter_descendants():
        if not _is_button_like(node):
            continue
        text = _button_text(node)
        if not text:
            continue
        identifier, source = _identity(node)
        candidates.append({
            "tag": node.tag, "text": text,
            "identifier": identifier, "identifier_source": source,
        })
    chosen = None
    for keyword in _ADVANCE_KEYWORDS:
        chosen = next((c for c in candidates if keyword.lower() in c["text"].lower()), None)
        if chosen is not None:
            break
    return {"chosen": chosen, "candidates": candidates}


# ---------------------------------------------------------------------------------
# Suspected dependent selects: present, but carrying only a placeholder.
# ---------------------------------------------------------------------------------

def _suspected_dependents(select_fields: list[dict]) -> list[dict]:
    suspects = []
    for field in select_fields:
        options = field["options"]
        real_option_count = sum(1 for option in options if not option["is_placeholder"])
        if real_option_count == 0:
            placeholder_label = next(
                (option["label"] for option in options if option["is_placeholder"]), None,
            )
            suspects.append({
                "identifier": field["identifier"],
                "identifier_source": field["identifier_source"],
                "evidence": {"option_count": len(options), "placeholder_label": placeholder_label},
            })
    return suspects


# ---------------------------------------------------------------------------------
# Shared walk -> the two public entry points.
# ---------------------------------------------------------------------------------

def _observe(root: _Node) -> tuple[dict, list[_Node]]:
    field_nodes = [node for node in root.iter_descendants() if node.tag in {"input", "textarea", "select"}]
    fields = [_build_field(node) for node in field_nodes]
    select_fields = [field for field in fields if field["tag"] == "select"]
    steps = _build_steps_report(_detect_wizard(root), field_nodes, fields)
    report = {
        "version": 1,
        "steps": steps,
        "advance_control": _detect_advance_control(root),
        "dependent_selects": _suspected_dependents(select_fields),
        "fields": fields,
    }
    return report, field_nodes


def observe_html(html: str) -> dict:
    """Everything derivable from static markup alone. No browser, no network."""
    root = _parse_tree(html)
    report, _field_nodes = _observe(root)
    return report


_HIDING_ANCESTOR_SCRIPT = """
(el) => {
    let node = el.parentElement;
    while (node) {
        const style = window.getComputedStyle(node);
        if (style.display === 'none' || style.visibility === 'hidden'
                || parseFloat(style.opacity || '1') === 0) {
            return { tag: node.tagName.toLowerCase(), class: node.className || null };
        }
        node = node.parentElement;
    }
    return null;
}
"""


def _attach_live_facts(page: Any, field: dict, node: _Node) -> None:
    selector = node.css_path()
    try:
        locator = page.locator(selector)
        visible = locator.is_visible()
        box = locator.bounding_box()
    except Exception as error:  # the live page is the one source of truth this can't fake
        field["visible"] = None
        field["bounding_box"] = None
        field["hiding_ancestor"] = None
        field["live_observation_error"] = str(error)
        return
    field["visible"] = visible
    field["bounding_box"] = box
    zero_sized = bool(box) and (box.get("width", 0) == 0 or box.get("height", 0) == 0)
    if visible is False or zero_sized:
        try:
            field["hiding_ancestor"] = locator.evaluate(_HIDING_ANCESTOR_SCRIPT)
        except Exception:
            field["hiding_ancestor"] = None
    else:
        field["hiding_ancestor"] = None


def _confirm_dependents(page: Any, report: dict, confirm: Sequence[Mapping[str, str]]) -> None:
    """Confirm a suspected dependent by driving one *other* select and re-reading its option
    count. Only runs when a caller passes ``confirm`` explicitly -- this module never drives
    the page on its own initiative."""
    by_identifier = {entry["identifier"]: entry for entry in report["dependent_selects"]}
    for spec in confirm:
        dependent_id = spec.get("dependent")
        entry = by_identifier.get(dependent_id)
        if entry is None:
            continue
        before = entry["evidence"]["option_count"]
        try:
            page.locator(f'[name="{spec["trigger"]}"], #{spec["trigger"]}').select_option(spec["value"])
            after = page.locator(f'[name="{dependent_id}"], #{dependent_id}').locator("option").count()
        except Exception as error:
            entry["confirmed"] = None
            entry["confirmation_error"] = str(error)
            continue
        entry["confirmed"] = after > before
        entry["evidence"]["option_count_after_trigger"] = after


def observe_page(page: Any, *, confirm_dependents: Sequence[Mapping[str, str]] | None = None) -> dict:
    """Everything ``observe_html`` reports, plus what only a live DOM knows: computed
    visibility, bounding boxes, and (for a hidden field) the ancestor causing it.

    ``confirm_dependents`` is optional and off by default: pass a list of
    ``{"trigger": <select identifier>, "value": <option value to choose>, "dependent": <select
    identifier>}`` to have this drive exactly those selects and report each named dependent's
    option count before and after. Without it, dependents stay *suspected*, never confirmed.
    """
    html = page.content()
    root = _parse_tree(html)
    report, field_nodes = _observe(root)
    for field, node in zip(report["fields"], field_nodes):
        _attach_live_facts(page, field, node)
    if confirm_dependents:
        _confirm_dependents(page, report, confirm_dependents)
    return report


# ---------------------------------------------------------------------------------
# diff_vocabulary: the reusable form of the check that would have caught a whole catalogue's
# worth of unmapped category names at once.
# ---------------------------------------------------------------------------------

def diff_vocabulary(observed_options: Sequence[str], claimed_values: Mapping[str, str]) -> dict:
    """Compare a select's real option labels against what some config claims for each identifier.

    Plain string similarity only (``difflib``, stdlib) -- this is deliberately not a model call;
    it is the mechanical check a lane's own test suite runs against its own catalogue overrides.
    """
    observed = [str(option) for option in observed_options]
    present: dict[str, str] = {}
    absent: dict[str, dict] = {}
    for identifier, claimed in claimed_values.items():
        claimed_text = str(claimed)
        if claimed_text in observed:
            present[identifier] = claimed_text
            continue
        matches = difflib.get_close_matches(claimed_text, observed, n=1, cutoff=0.0)
        closest = matches[0] if matches else None
        similarity = difflib.SequenceMatcher(None, claimed_text, closest).ratio() if closest else None
        absent[identifier] = {"claimed": claimed_text, "closest": closest, "similarity": similarity}
    return {"present": present, "absent": absent}
