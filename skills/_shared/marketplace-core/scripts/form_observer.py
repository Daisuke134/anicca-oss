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
    "clickable_controls",
    "clickable_accessible_names",
    "clickable_public_record",
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

# A second, label-driven placeholder convention alongside the position/empty-value one below: a
# marketplace observed live carried a select whose currently-selected option had a real,
# non-empty `value` attribute yet still meant "nothing chosen" -- its label read 未選択, and a
# sibling select's own placeholder option read 業務を選択してください. Neither word is specific
# to any one marketplace's build (this module stays platform-neutral, see the test that asserts
# no platform name ever appears here); both are generic Japanese "please choose"/"unselected"
# phrasing a <select>'s own placeholder option commonly carries. A label merely containing one of
# these words -- not just equal to it -- is treated as a placeholder, since a marketplace's own
# copy sometimes wraps the word in a longer prompt ("業務を選択してください").
_PLACEHOLDER_LABEL_WORDS = ("未選択", "選択してください")


def _is_placeholder_label(label: str) -> bool:
    return any(word in (label or "") for word in _PLACEHOLDER_LABEL_WORDS)


def _select_options(node: _Node) -> tuple[list[dict], int | None]:
    option_nodes = [child for child in node.children if child.tag == "option"]
    options = [{"value": opt.attrs.get("value"), "label": opt.text} for opt in option_nodes]
    # The placeholder convention on every marketplace this has been checked against: an
    # empty or absent value on the first option. A select with real choices in that first
    # slot (value present and non-empty) has no placeholder to identify structurally -- but
    # see _is_placeholder_label above for the second, label-driven convention this still catches.
    placeholder_index = 0 if options and (options[0]["value"] in (None, "")) else None
    for index, option in enumerate(options):
        option["is_placeholder"] = index == placeholder_index or _is_placeholder_label(option["label"])
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
# Step requirements: every control the page itself marks required or optional, read generically
# from the DOM outward -- never from a caller's list of field names it already knows to fill. A
# native `required`/`aria-required="true"` attribute is one marker a form might use; the other,
# just as real, is a visible badge sitting next to a field's label -- a form actually observed
# live reads "タイトル 必須 … サブタイトル 任意 … カテゴリー 必須 …", badge and label side by
# side. This section reads that second marker: it is what lets a required control neither this
# module nor its caller was ever told to expect still show up in the report, with its own visible
# label. Live-only (observe_page, never observe_html): "which control is on the step actually
# showing" and "does it hold a value" are both facts only a live DOM knows.
# ---------------------------------------------------------------------------------

_REQUIRED_BADGE_WORDS = {"必須": True, "任意": False}


def _requirement_badges(root: _Node) -> list[_Node]:
    """Every element whose own direct text (not any descendant's) is exactly a badge word.
    ``_Node.text`` already isolates text nodes that belong to this element itself -- the same
    property ``_step_name`` above relies on -- so a wrapping container whose *aggregate* text
    happens to contain "必須" is never mistaken for the badge itself."""
    return [node for node in root.iter_descendants() if node.text in _REQUIRED_BADGE_WORDS]


def _first_native_control(node: _Node) -> _Node | None:
    for descendant in node.iter_descendants():
        if descendant.tag in {"input", "textarea", "select"}:
            return descendant
    return None


def _requirement_row(badge: _Node, root: _Node) -> _Node:
    """The markup uniquely owning `badge`: climb from the badge upward while the ancestor still
    contains exactly this one required/optional badge, stopping at the highest ancestor for which
    that still holds. Bounding the row by badge *count*, rather than by "does this subtree contain
    a control" (which a whole multi-field step trivially does), is what keeps this from wandering
    into a sibling field's control once climbing one level further would start covering two badges
    at once -- the same sibling-counting spirit `_hiding_class_signature` above already uses for a
    different structural question.
    """
    row = badge
    node = badge
    while node is not root and node.parent is not None:
        parent = node.parent
        if len(_requirement_badges(parent)) > 1:
            break
        row = parent
        if parent is root:
            break
        node = parent
    return row


def _requirement_label(row: _Node, control: _Node | None, badge_word: str) -> str:
    """`row`'s own visible text, minus whatever `control` itself contributes (a <select>'s option
    labels, if `control` is the row's own select) and minus the badge word -- what remains is the
    label a user would actually read next to the field."""
    parts: list[str] = []

    def _walk(node: _Node) -> None:
        if node is control:
            return
        parts.extend(node.text_parts)
        for child in node.children:
            _walk(child)

    _walk(row)
    text = "".join(parts).replace(badge_word, " ")
    return " ".join(text.split())


def _step_requirement_candidates(root: _Node) -> list[dict]:
    """Structural half of step_requirements -- everything derivable without a live page. Each
    candidate carries the row/control nodes under private keys (`_row`/`_control`) for the live
    pass (`_step_requirements` in observe_page) to resolve visibility and value from; those two
    keys are popped before a candidate ever reaches the public report.
    """
    candidates = []
    for badge in _requirement_badges(root):
        required = _REQUIRED_BADGE_WORDS[badge.text]
        row = _requirement_row(badge, root)
        control = _first_native_control(row)
        label = _requirement_label(row, control, badge.text)
        identifier, source = _identity(control) if control is not None else (None, "none")
        candidates.append({
            "identifier": identifier,
            "identifier_source": source,
            "label": label,
            "required": required,
            "tag": control.tag if control is not None else None,
            # A control this couldn't resolve to any native input/textarea/select is a custom
            # widget -- present, but this module has no generic way to read its value, so it is
            # reported unreadable rather than assumed filled (see _requirement_filled below).
            "readable": control is not None,
            "_row": row,
            "_control": control,
        })
    return candidates


def _requirement_filled(page: Any, control: _Node | None) -> bool | None:
    """Whether `control` currently holds a value, read generically off the live page -- never
    from any caller's idea of what the field is named. A <select> counts only a non-placeholder
    `option:checked` as filled (mirrors `_create_selected_option`'s convention elsewhere in this
    house: an unset native <select> still reports its first/placeholder option as checked, which
    must read as empty, not unreadable). "Non-placeholder" is decided the same way
    `_select_options` above decides it: an empty `value` attribute, *or* a label containing one of
    _PLACEHOLDER_LABEL_WORDS -- a marketplace observed live had a select whose checked option
    carried a real, non-empty value yet still meant nothing chosen, distinguishable only by its
    label reading 未選択. Checking the value alone (the original, narrower form of this function)
    reported that select `filled: true` -- a false negative for exactly the failure this function
    exists to catch. A control this cannot resolve at all -- including "no native control found
    for this badge" (`control is None`) -- reports None: an unmeasured fact must never look like a
    negative one, exactly as this module's own module docstring says of `observe_html`'s omitted
    live-only fields.
    """
    if control is None:
        return None
    try:
        locator = page.locator(control.css_path())
        if control.tag == "select":
            checked = locator.locator("option:checked")
            if checked.count() != 1:
                return None
            option = checked.all()[0]
            value = option.get_attribute("value") or ""
            label = " ".join(str(option.inner_text() or "").split())
            if _is_placeholder_label(label):
                return False
            return bool(value.strip())
        value = locator.input_value()
        return bool(value and str(value).strip())
    except Exception:
        return None


def _step_requirements(page: Any, root: _Node) -> list[dict]:
    """Every required/optional control the *visible* step actually carries. "Visible" is decided
    the same way the rest of observe_page already decides it for ordinary fields -- a live
    `Locator.is_visible()` check -- so a control belonging to a hidden step (or a candidate this
    module's own live check could not resolve at all) is silently excluded, never reported with a
    guessed visibility.
    """
    results = []
    for candidate in _step_requirement_candidates(root):
        row = candidate.pop("_row")
        control = candidate.pop("_control")
        try:
            visible = page.locator(row.css_path()).is_visible()
        except Exception:
            visible = None
        if visible is not True:
            continue
        candidate["filled"] = _requirement_filled(page, control)
        results.append(candidate)
    return results


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
    # Live-only, like visible/bounding_box/hiding_ancestor above -- absent from observe_html's
    # report rather than guessed, since "which step is visible" and "does a control hold a
    # value" are both facts only a live DOM knows.
    report["step_requirements"] = _step_requirements(page, root)
    return report


# ---------------------------------------------------------------------------------
# Clickable-control census: every visible, plausibly-clickable control on a live page, with every
# accessible-name source a caller might match a label against.
#
# This shipped from a marketplace wake whose wizard-advance search could not find its own final
# step's own submit control: the search looked only at <button> elements' own visible text, so a
# control expressed as an <a>, an input[type=submit]/input[type=button], or a [role="button"]
# element -- or a real <button> whose accessible name lives in an aria-label, a title, or a
# value rather than in its own text -- was invisible to it. The one visible <button> that search
# did find carried no text at all; the real control was almost certainly one of these other
# shapes, but the search had no way to say so because it never looked.
#
# clickable_controls() is the fix: one census covering every shape a "move this listing forward"
# control has actually been observed to take, live-only (Playwright, never observe_html, since
# visibility is exactly what decides which control on a multi-step wizard is real right now).
# Each record already resolves to a genuinely interactive element -- never a bare text node
# needing an ancestor climb -- so a caller matches directly against clickable_accessible_names()
# and either gets exactly one control or a named failure carrying the whole census; it never
# falls back to "the only visible control".
# ---------------------------------------------------------------------------------

_CLICKABLE_TAG_SELECTORS: tuple[tuple[str | None, str], ...] = (
    ("button", "button"),
    ("a", "a"),
    ("input", 'input[type="submit"]'),
    ("input", 'input[type="button"]'),
    (None, '[role="button"]'),
)
_CLICKABLE_OUTER_HTML_MAX_CHARS = 300
_CLICKABLE_TRUNCATION_MARKER = "...(truncated)"
# The only sources this module will ever say name a control -- deliberately excludes img_alt:
# an <img>'s alt text describes the image, not necessarily the control's own action, and no live
# marketplace form has ever been observed relying on it to name a control. img_alt is still
# carried on every record (see _clickable_record) so a human reading a failure report can see it;
# it is simply never one of the sources a caller matches a label against.
_CLICKABLE_ACCESSIBLE_NAME_KEYS = ("text", "aria_label", "title", "value")


def _clickable_safe(fn, default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        return default


def _clickable_hard_truncate(value: Any, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + _CLICKABLE_TRUNCATION_MARKER


def _clickable_img_alt(element: Any) -> str | None:
    """A nested <img>'s alt text, when this control has one -- the fact that lets a button with
    no text of its own (an icon-only control) still be identified in a failure report. Reported
    for identification only; see the module comment above for why it is never a match source."""
    try:
        images = element.locator("img")
        if images.count() < 1:
            return None
        return images.all()[0].get_attribute("alt")
    except Exception:
        return None


def _clickable_record(element: Any, known_tag: str | None) -> dict[str, Any]:
    """Everything this module can say about one clickable element: which accessible-name sources
    it carries (text/aria-label/title/value, plus img_alt for identification only -- see
    _CLICKABLE_ACCESSIBLE_NAME_KEYS), its tag and type, and a hard-truncated outerHTML so a human
    can tell at a glance whether an unmatched control is a real submit button or something else
    entirely. `known_tag` is the tag the selector that found this element already names (button/
    a/input); only the generic [role="button"] selector needs a live tagName read. Every fact is
    independent and best-effort -- one field this can't read off a given element reports None
    rather than failing the whole record.
    """
    tag = known_tag or _clickable_safe(lambda: element.evaluate("el => el.tagName.toLowerCase()"))
    text = " ".join(str(_clickable_safe(lambda: element.inner_text(), "") or "").split())
    return {
        "tag": tag,
        "type": _clickable_safe(lambda: element.get_attribute("type")),
        "text": text,
        "aria_label": _clickable_safe(lambda: element.get_attribute("aria-label")),
        "title": _clickable_safe(lambda: element.get_attribute("title")),
        "value": _clickable_safe(lambda: element.get_attribute("value")),
        "img_alt": _clickable_img_alt(element),
        "outer_html": _clickable_hard_truncate(
            _clickable_safe(lambda: element.evaluate("el => el.outerHTML")),
            _CLICKABLE_OUTER_HTML_MAX_CHARS,
        ),
        # Popped by clickable_public_record() before a census ever reaches a serialized report --
        # kept here so the one function that built this record is also the one a caller can click
        # through without a second, separate lookup back into the live page.
        "_element": element,
    }


def clickable_controls(page: Any) -> list[dict[str, Any]]:
    """Census of every visible, plausibly-clickable control on the live page: <button>, <a>,
    input[type=submit], input[type=button], and [role="button"] -- the reach a "move this listing
    forward" control has actually been observed taking on a real marketplace wizard (see the
    module comment above). Never raises: a selector this page's own Playwright surface cannot
    resolve, or one element this cannot read a visibility/text/attribute fact from, is simply
    excluded or reported with that one fact missing -- never the reason the whole census fails.
    """
    records: list[dict[str, Any]] = []
    for known_tag, selector in _CLICKABLE_TAG_SELECTORS:
        try:
            elements = page.locator(selector).all()
        except Exception:
            continue
        for element in elements:
            if not _clickable_safe(lambda: element.is_visible(), False):
                continue
            records.append(_clickable_record(element, known_tag))
    return records


def clickable_accessible_names(record: Mapping[str, Any]) -> list[str]:
    """Every accessible-name source a caller may match one of clickable_controls()'s records
    against: visible text, aria-label, title, and (for an input[type=submit|button]) its value --
    trimmed, empties excluded. Deliberately excludes img_alt; see the module comment above."""
    names = []
    for key in _CLICKABLE_ACCESSIBLE_NAME_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    return names


def clickable_public_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """`record` with its private `_element` popped -- the JSON-safe shape a failure report
    serializes, never the raw census a caller searches against."""
    return {key: value for key, value in record.items() if not key.startswith("_")}


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
