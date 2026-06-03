# tests for the home page logic (alignment, badge color, X/indicator layout)
import pytest
from unittest.mock import MagicMock, patch

from nixos_gui.state import AppState, IntentType, Intent
from nixos_gui.pages import home


def _intent(it: IntentType, target: str, value=None) -> Intent:
    return Intent(intent_type=it, target=target, value=value)


def test_count_badge_color_is_white():
    # the count badge next to 'Pending Changes' must use white text (not dark navy)
    s, st = MagicMock(), MagicMock()
    s.__getitem__.side_effect = lambda k: {
        "text": "#fafafa", "accent": "#8b5cf6",
    }[k]
    s.card_padding = (1, 1.25, 1, 1.25)
    st.pending_intents = [_intent(IntentType.ADD_PACKAGE, "nginx")]
    st.config_reader.get_hostname.return_value = "laptop"

    import inspect, re
    src = inspect.getsource(home)
    m = re.search(r'hd\.badge\(str\(count\)[^)]+\)', src)
    assert m is not None, "count badge block not found"
    block = m.group(0)
    assert "font_color" in block
    color = re.search(r'font_color="?(\#\w+|[\w\(\),.\s]+)"?', block)
    assert color is not None
    assert "1a1a2e" not in color.group(0), \
        f"count badge uses dark text ({color.group(0)}); should be white"


def test_x_button_is_on_the_left_of_text():
    # the X remove button should be on the LEFT of the intent label, not the right
    import inspect, re
    src = inspect.getsource(home)
    icon_btn_match = re.search(r'hd\.icon_button\(\s*"x"', src)
    # label is now hd.text (plain text for all rows)
    label_match = re.search(r'hd\.text\(\s*f"\{sign\}', src)
    assert icon_btn_match and label_match
    assert icon_btn_match.start() < label_match.start(), \
        "X button should come before the intent label in the row"


def test_add_package_uses_green_indicator():
    # an add_package intent should use the success color (green)
    import inspect, re
    src = inspect.getsource(home)
    m = re.search(r'itype == "add_package".*?color = s\["success"\]', src, re.DOTALL)
    assert m is not None, \
        "add_package intent should use s['success'] (green)"


def test_remove_package_uses_red_indicator():
    # remove_package intent should use danger color (red)
    import inspect, re
    src = inspect.getsource(home)
    m = re.search(r'itype == "remove_package".*?color = s\["danger"\]', src, re.DOTALL)
    assert m is not None, \
        "remove_package intent should use s['danger'] (red)"


def test_set_option_shows_old_to_new():
    # set_option intents should show - old / + new (git-diff style) when expanded
    import inspect, re
    src = inspect.getsource(home)
    m = re.search(r'f"- \{_fmt_val\(old_val\)\}".*?f"\+ \{_fmt_val\(intent\.value\)\}"', src, re.DOTALL)
    assert m is not None, \
        "set_option expanded view should display git-diff - old / + new format"


def test_list_diff_uses_colored_spans():
    # list diffs should show removed items in red and added items in green
    import inspect, re
    src = inspect.getsource(home)
    # check that removed items get danger color and added items get success color
    removed_match = re.search(r'item_str in removed.*?font_color=s\["danger"\]', src, re.DOTALL)
    added_match = re.search(r'item_str in added.*?font_color=s\["success"\]', src, re.DOTALL)
    assert removed_match, "removed list items should be colored red"
    assert added_match, "added list items should be colored green"
