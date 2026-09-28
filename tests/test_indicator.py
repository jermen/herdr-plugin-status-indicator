import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import indicator as app
import install

NOW = 1_800_000_000


def pane(**fields):
    return {"pane_id": "w1:p1", "agent": "codex", "agent_status": "working", "focused": False,
            "agent_session": {"kind": "id", "value": "session-a"}, "terminal_id": "term1",
            "terminal_title_stripped": "DMDOX-317: Status indicator", "tokens": {}, **fields}


def usage():
    return {"schema_version": 1, "generated_at": NOW, "panes": [{"pane_id": "w1:p1",
             "session_key": "codex:session-a", "collection_status": "ok"}],
            "sessions": [{"key": "codex:session-a", "active": True, "current": {
                "limits": [{"name": "primary", "used_percent": 7, "window_minutes": 300, "resets_at": NOW + 300},
                           {"name": "secondary", "used_percent": 55, "window_minutes": 10080, "resets_at": NOW + 3600}],
                "context": {"used_percent": 14}, "metric_observed_at": {"limits": NOW}}, "periods": {}}]}


class UsageTests(unittest.TestCase):
    def test_default_keeps_quota_columns_without_provider_limits(self):
        for agent in ("codex", "claude", "kimi"):
            with self.subTest(agent=agent):
                data = usage()
                data["panes"][0]["session_key"] = agent + ":session-a"
                session = data["sessions"][0]
                session["key"] = agent + ":session-a"
                session["current"].pop("limits")
                session["periods"]["last_7_days"] = {"usage_records_observed": 1,
                    "estimated_spend": {"currency": "USD", "amount": "18.44", "coverage": "complete"}}
                self.assertEqual(app.details(pane(agent=agent), data, {}, NOW),
                                 "h -- / w -- / ctx 14 %")
                session["current"].pop("context")
                self.assertEqual(app.details(pane(agent=agent), data, {}, NOW),
                                 "h -- / w -- / ctx --")

    def test_api_key_claude_shows_session_cost_instead_of_quota(self):
        data = usage()
        data["panes"][0]["session_key"] = "claude:session-a"
        session = data["sessions"][0]
        session["key"] = "claude:session-a"
        current = session["current"]
        limits = current["limits"]
        current.update(limits=[], session_estimated_cost_usd=2.4512)
        p = pane(agent="claude")
        self.assertEqual(app.details(p, data, {}, NOW), "$2.45 / ctx 14 %")
        self.assertEqual(app.details(p, data, {"mode": "estimated"}, NOW), "d -- / w -- / m -- / ctx 14 %")
        # No cost before the first response, when a subscription has no quota yet either.
        current["session_estimated_cost_usd"] = 0
        self.assertEqual(app.details(p, data, {}, NOW), "h -- / w -- / ctx 14 %")
        # An idle subscription whose windows expired keeps its quota columns.
        current["session_estimated_cost_usd"] = 81
        session["periods"]["last_31_days"] = {"quota_samples": [
            {"at": NOW - 86400, "windows": [{"name": "five_hour", "used_percent": 10}]},
            {"at": NOW, "windows": []}]}
        self.assertEqual(app.details(p, data, {}, NOW), "h -- / w -- / ctx 14 %")
        current["limits"] = limits
        self.assertEqual(app.details(p, data, {}, NOW), "h 7 % / w 55 % / ctx 14 %")

    def test_auto_is_an_explicit_spending_fallback(self):
        data = usage()
        config = {"mode": "auto"}
        self.assertEqual(app.details(pane(), data, config, NOW), "h 7 % / w 55 % / ctx 14 %")
        data["sessions"][0]["current"].pop("limits")
        self.assertEqual(app.details(pane(), data, config, NOW), "d -- / w -- / m -- / ctx 14 %")

    def test_quota_percentages_are_used_not_remaining(self):
        self.assertEqual(app.details(pane(), usage(), {}, NOW), "h 7 % / w 55 % / ctx 14 %")

    def test_primary_can_be_weekly(self):
        data = usage()
        data["sessions"][0]["current"]["limits"] = [{"name": "primary", "used_percent": 55, "window_minutes": 10080}]
        self.assertEqual(app.details(pane(), data, {}, NOW), "h -- / w 55 % / ctx 14 %")

    def test_stale_snapshot_hides_values(self):
        self.assertEqual(app.details(pane(), usage(), {}, NOW + 121), "usage stale")

    def test_expired_window_is_not_zero(self):
        data = usage()
        data["sessions"][0]["current"]["limits"][0]["resets_at"] = NOW - 1
        self.assertIn("h -- / w 55 %", app.details(pane(), data, {}, NOW))

    def test_fresh_snapshot_does_not_refresh_idle_quota(self):
        data = usage()
        data["sessions"][0]["current"]["metric_observed_at"]["limits"] = NOW - 1000
        self.assertEqual(app.details(pane(), data, {}, NOW), "h 7 % / * / w 55 % / * / ctx 14 %")

    def test_session_replaced_in_same_pane(self):
        p = pane(agent_session={"kind": "id", "value": "new-session"})
        self.assertEqual(app.details(p, usage(), {}, NOW), "usage unavailable")

    def test_no_cwd_or_active_session_guess(self):
        for p in [pane(agent_session=None), pane(pane_id="w2:p1"), pane(agent="claude")]:
            self.assertEqual(app.details(p, usage(), {}, NOW), "usage unavailable")

    def test_unknown_schema_and_unavailable(self):
        for data in [None, {}, {"schema_version": 2}]:
            self.assertEqual(app.details(pane(), data, {}, NOW), "usage unavailable")

    def test_estimates_are_marked_and_periods_are_exact(self):
        data = usage()
        session = data["sessions"][0]
        session["current"].pop("limits")
        for period, amount in zip(("today", "last_7_days", "last_31_days"), ("15", "89", "622")):
            session["periods"][period] = {"usage_records_observed": 1, "estimated_spend": {
                "currency": "USD", "amount": amount, "coverage": "complete"}}
        result = app.details(pane(), data, {"mode": "estimated"}, NOW)
        self.assertEqual(result, "d ~$15.00 / w ~$89.00 / m ~$622.00 / ctx 14 %")

    def test_missing_spend_and_zero_observations_are_unknown(self):
        data = usage()
        data["sessions"][0]["periods"]["today"] = {"usage_records_observed": 0,
                "estimated_spend": {"amount": "0", "currency": "USD", "coverage": "complete"}}
        self.assertIn("d -- / w -- / m --", app.details(pane(), data, {"mode": "estimated"}, NOW))

    def test_partial_estimate_is_visible(self):
        data = usage()
        data["sessions"][0]["periods"]["today"] = {"usage_records_observed": 2,
                "estimated_spend": {"amount": "1.235", "currency": "USD", "coverage": "partial"}}
        self.assertIn("d ~$1.24*", app.details(pane(), data, {"mode": "estimated"}, NOW))

    def test_billed_is_explicit_and_never_falls_back_to_estimate(self):
        data = usage()
        data["sessions"][0]["periods"]["today"] = {"billed_spend": {"currency": "USD", "amount": "2.12"}}
        result = app.details(pane(), data, {"mode": "billed"}, NOW)
        self.assertIn("d $2.12 / w -- / m -- / reported", result)

    def test_agent_mode_overrides_default(self):
        self.assertIn("d --", app.details(pane(), usage(), {"mode": "quota", "modes": {"codex": "billed"}}, NOW))

    def test_collection_failure_remains_visible(self):
        data = usage()
        data["panes"][0]["collection_status"] = "catching_up"
        self.assertIn("usage catching_up", app.details(pane(), data, {}, NOW))

    def test_missing_api_transcript_reason_replaces_empty_amounts(self):
        p = pane(agent="claude", tokens={"subagents_running": 2})
        for status, label in (("transcript_missing", "transcript missing"),
                              ("transcript_ambiguous", "multiple transcripts"),
                              ("transcript_error", "transcript read error")):
            with self.subTest(status=status):
                data = usage()
                data["panes"][0].update(session_key="claude:session-a", collection_status=status)
                data["sessions"][0].update(key="claude:session-a", current={})
                tokens = app.Renderer({"mode": "estimated"}).render({"panes": [p]}, data, NOW)[p["pane_id"]]
                self.assertEqual(tokens["si_usage"], label)
                self.assertEqual(tokens["si_notes"], "2 subagents")
                self.assertEqual(tokens["si_detail"], label + " / 2 subagents")
                self.assertNotIn("d --", tokens["si_detail"])

    def test_transcript_failure_keeps_collected_amounts_and_context(self):
        data = usage()
        data["panes"][0]["collection_status"] = "transcript_missing"
        session = data["sessions"][0]
        session["current"].pop("limits")
        session["periods"]["today"] = {"usage_records_observed": 1,
            "estimated_spend": {"currency": "USD", "amount": "0.12", "coverage": "complete"}}
        self.assertEqual(app.details(pane(), data, {"mode": "estimated"}, NOW),
                         "d ~$0.12 / w -- / m -- / ctx 14 % / transcript missing")

    def test_transcript_failure_keeps_available_quota(self):
        data = usage()
        data["panes"][0]["collection_status"] = "transcript_missing"
        self.assertEqual(app.details(pane(), data, {}, NOW),
                         "h 7 % / w 55 % / ctx 14 % / transcript missing")

    def test_subagents_require_explicit_metadata(self):
        self.assertNotIn("subagent", app.details(pane(), usage(), {}, NOW))
        for count in ("2", "0", "1"):
            self.assertIn(count + " subagent", app.details(pane(tokens={"subagents_running": count}), usage(), {}, NOW))
        for value in ("-1", "maybe", "2 running", True):
            self.assertNotIn("subagent", app.details(pane(tokens={"subagents_running": value}), usage(), {}, NOW))

    def test_invalid_numbers_never_render_nan_or_inf(self):
        for value in (None, float("nan"), float("inf"), -1, 101, True, "7"):
            self.assertEqual(app.percent(value), "--")
        for value in (None, "NaN", "Infinity", "-2", True):
            self.assertEqual(app.money(value), "--")


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.renderer = app.Renderer({"icons": "font"})

    def render(self, p):
        return self.renderer.render({"panes": [p]}, usage(), NOW)[p["pane_id"]]

    def test_focus_does_not_change_titles(self):
        first, second = pane(), pane(pane_id="w1:p2", terminal_title_stripped="Another pane")
        a = self.renderer.render({"panes": [first, second], "tabs": [{"label": "First"}]}, usage(), NOW)
        first["focused"], second["focused"] = False, True
        b = self.renderer.render({"panes": [first, second], "tabs": [{"label": "Second"}]}, usage(), NOW)
        self.assertEqual(a, b)

    def test_each_state_has_one_token_and_glyph(self):
        for state in app.STATES:
            renderer = app.Renderer({"icons": "font"})
            row = renderer.render({"panes": [pane(agent_status=state)]}, usage(), NOW)["w1:p1"]
            self.assertEqual(["si_" + s for s in app.STATES if row["si_" + s]], ["si_" + state])
            self.assertIn(app.LOGOS["codex"], row["si_" + state])

    def test_done_latches_until_focused(self):
        self.render(pane(agent_status="done"))
        self.assertIsNotNone(self.render(pane(agent_status="idle"))["si_done"])
        self.assertIsNotNone(self.render(pane(agent_status="idle", focused=True))["si_idle"])

    def test_question_latches_until_work_resumes(self):
        self.render(pane(agent_status="blocked"))
        self.assertIsNotNone(self.render(pane(agent_status="idle", focused=True))["si_blocked"])
        self.assertIsNotNone(self.render(pane(agent_status="working"))["si_working"])

    def test_latch_is_not_inherited_by_new_session(self):
        self.render(pane(agent_status="blocked"))
        self.assertIsNotNone(self.render(pane(agent_status="idle", agent_session={"value": "new"}))["si_idle"])

    def test_control_characters_do_not_escape_title(self):
        row = self.render(pane(label="Safe\n\x1b[31mred\x1b[0m\x07\u202e"))["si_working"]
        self.assertNotIn("\x1b", row)
        self.assertNotIn("\n", row)
        self.assertIn("Safe red", row)

    def test_missing_title_reuses_only_same_session_title(self):
        self.render(pane(label="Pinned"))
        self.assertIn("Pinned", self.render(pane(terminal_title_stripped=""))["si_working"])
        self.assertNotIn("Pinned", self.render(pane(terminal_title_stripped="", agent_session={"value":"new"}))["si_working"])

    def test_publish_preserves_unowned_metadata_and_avoids_rewrites(self):
        p = pane()
        row = self.render(p)
        p["tokens"] = {**row, "someone_else": "retained"}
        with patch.object(app, "call") as call:
            app.publish("unused", {p["pane_id"]: row}, {"panes": [p]})
            call.assert_not_called()
            p["agent"] = None
            app.publish("unused", {}, {"panes": [p]})
            params = call.call_args.args[2]
            self.assertNotIn("someone_else", params["tokens"])
            self.assertNotIn("title", params)
            self.assertTrue(all(value is None for value in params["tokens"].values()))


class UsageColorTests(unittest.TestCase):
    def render(self, data, p=None, config=None, now=NOW):
        return app.Renderer(config or {}).render({"panes": [p or pane()]}, data, now)["w1:p1"]

    def visible(self, tokens):
        row = tomllib.loads(install.sidebar())["ui"]["sidebar"]["agents"]["rows"][1]
        values = []
        for entry in row:
            name = entry if isinstance(entry, str) else entry["token"]
            value = tokens.get(name.removeprefix("$"))
            if value:
                values.append((value, None if isinstance(entry, str) else entry.get("fg")))
        return values

    def test_each_percentage_uses_strict_unrounded_thresholds(self):
        for index, label in enumerate(("h", "w", "ctx")):
            for value, color in ((0, None), (40, None), (40.01, "#c78a1f"),
                                 (80, "#c78a1f"), (80.01, "#c04a4a"), (100, "#c04a4a")):
                with self.subTest(label=label, value=value):
                    data = usage()
                    current = data["sessions"][0]["current"]
                    metric = current["context"] if label == "ctx" else current["limits"][index]
                    metric["used_percent"] = value
                    values = self.visible(self.render(data))
                    matches = [(text, fg) for text, fg in values if text.startswith(label + " ")]
                    self.assertEqual(matches, [(label + " " + app.percent(value), color)])

    def test_fields_have_independent_colors_and_preserve_plain_layout(self):
        data = usage()
        data["sessions"][0]["current"]["context"]["used_percent"] = 81
        tokens = self.render(data)
        self.assertEqual(self.visible(tokens), [("h 7 %", None), ("w 55 %", "#c78a1f"),
                                               ("ctx 81 %", "#c04a4a")])
        self.assertEqual(tokens["si_detail"], "h 7 % / w 55 % / ctx 81 %")

    def test_unavailable_and_invalid_values_are_neutral(self):
        for value in (None, True, "90", -1, 101, float("nan"), float("inf")):
            with self.subTest(value=value):
                data = usage()
                current = data["sessions"][0]["current"]
                for metric in [*current["limits"], current["context"]]:
                    metric["used_percent"] = value
                self.assertEqual(self.visible(self.render(data)),
                                 [("h --", None), ("w --", None), ("ctx --", None)])

    def test_expired_quota_is_neutral_even_if_last_value_was_high(self):
        data = usage()
        metric = data["sessions"][0]["current"]["limits"][0]
        metric.update(used_percent=95, resets_at=NOW)
        self.assertEqual(self.visible(self.render(data))[0], ("h --", None))

    def test_spending_and_notes_stay_neutral_with_colored_context(self):
        data = usage()
        session = data["sessions"][0]
        session["current"]["context"]["used_percent"] = 90
        session["periods"]["today"] = {"usage_records_observed": 1,
            "estimated_spend": {"amount": "99", "currency": "USD", "coverage": "complete"}}
        data["panes"][0]["collection_status"] = "catching_up"
        tokens = self.render(data, pane(tokens={"subagents_running": 2}), {"mode": "estimated"})
        self.assertEqual(self.visible(tokens), [("d ~$99.00 / w -- / m --", None),
            ("ctx 90 %", "#c04a4a"), ("usage catching_up / 2 subagents", None)])

    def test_api_key_session_cost_is_neutral_and_replaces_quota_fields(self):
        data = usage()
        data["panes"][0]["session_key"] = "claude:session-a"
        session = data["sessions"][0]
        session["key"] = "claude:session-a"
        session["current"].update(limits=[], session_estimated_cost_usd="12.5")
        session["current"]["context"]["used_percent"] = 90
        tokens = self.render(data, pane(agent="claude"))
        self.assertEqual(self.visible(tokens), [("$12.50", None), ("ctx 90 %", "#c04a4a")])
        self.assertEqual(tokens["si_detail"], "$12.50 / ctx 90 %")

    def test_additional_quota_windows_and_staleness_keep_their_labels(self):
        data = usage()
        current = data["sessions"][0]["current"]
        current["limits"].append({"window_minutes": 1440, "used_percent": 99})
        current["metric_observed_at"]["limits"] = NOW - 1000
        current["limits"].append({"window_minutes": 120, "used_percent": 20})
        self.assertEqual(self.visible(self.render(data)), [("h 7 %", None), ("*", "#c04a4a"),
            ("w 55 %", "#c78a1f"), ("*", "#c04a4a"), ("d 99 % / 2h 20 %", None),
            ("*", "#c04a4a"), ("ctx 14 %", None)])

    def test_freshness_marker_preserves_value_and_context_colors(self):
        data = usage()
        current = data["sessions"][0]["current"]
        current["metric_observed_at"]["limits"] = NOW - 901
        current["context"]["used_percent"] = 90
        for value, color in ((0, None), (40, None), (55, "#c78a1f"), (81, "#c04a4a")):
            with self.subTest(value=value):
                current["limits"][0]["used_percent"] = value
                tokens = self.render(data)
                self.assertEqual(self.visible(tokens), [("h " + app.percent(value), color),
                    ("*", "#c04a4a"), ("w 55 %", "#c78a1f"), ("*", "#c04a4a"),
                    ("ctx 90 %", "#c04a4a")])
                self.assertNotIn("quota stale", tokens["si_detail"])

    def test_only_available_quota_values_receive_markers(self):
        data = usage()
        current = data["sessions"][0]["current"]
        current["metric_observed_at"]["limits"] = NOW - 901
        for unavailable in (None, float("nan"), -1, 101):
            with self.subTest(unavailable=unavailable):
                current["limits"][0]["used_percent"] = unavailable
                self.assertEqual(self.visible(self.render(data)), [("h --", None),
                    ("w 55 %", "#c78a1f"), ("*", "#c04a4a"), ("ctx 14 %", None)])
        current["limits"][0].update(used_percent=90, resets_at=NOW)
        self.assertIsNone(self.render(data)["si_h_stale"])
        current["limits"].pop(0)
        self.assertIsNone(self.render(data)["si_h_stale"])
        current["limits"] = []
        self.assertEqual(self.visible(self.render(data, config={"mode": "quota"})),
                         [("h --", None), ("w --", None), ("ctx 14 %", None)])

    def test_freshness_boundary_and_missing_timestamp(self):
        data = usage()
        current = data["sessions"][0]["current"]
        for age, marked in ((900, False), (901, True), (0, False)):
            current["metric_observed_at"]["limits"] = NOW - age
            self.assertEqual(self.render(data)["si_w_stale"], "*" if marked else None)
        current["metric_observed_at"]["limits"] = NOW - 301
        self.assertEqual(self.render(data, config={"quota_max_age": 300})["si_w_stale"], "*")
        current["metric_observed_at"].pop("limits")
        self.assertEqual(self.render(data)["si_w_stale"], "*")

    def test_fresh_data_clears_markers_without_changing_percentages(self):
        data = usage()
        current = data["sessions"][0]["current"]
        current["limits"].append({"window_minutes": 1440, "used_percent": 20})
        current["metric_observed_at"]["limits"] = NOW - 901
        p = pane(tokens={**self.render(data), "other": "preserved"})
        current["metric_observed_at"]["limits"] = NOW
        expected = self.render(data)
        with patch.object(app, "call") as call:
            app.publish("unused", {p["pane_id"]: expected}, {"panes": [p]})
            for invocation in call.call_args_list:
                p["tokens"].update(invocation.args[2]["tokens"])
        self.assertEqual(p["tokens"], {**expected, "other": "preserved"})
        self.assertTrue(all(p["tokens"][name] is None for name in
                            ("si_h_stale", "si_w_stale", "si_usage_stale")))

    def test_partial_pricing_asterisk_stays_neutral_and_has_no_quota_marker(self):
        data = usage()
        session = data["sessions"][0]
        session["current"]["metric_observed_at"]["limits"] = NOW - 901
        session["periods"]["today"] = {"usage_records_observed": 1, "estimated_spend": {
            "currency": "USD", "amount": "1.23", "coverage": "partial"}}
        tokens = self.render(data, config={"mode": "estimated"})
        self.assertEqual(self.visible(tokens), [("d ~$1.23* / w -- / m --", None), ("ctx 14 %", None)])

    def test_changes_clear_previous_color_and_stale_values(self):
        data = usage()
        p = pane()
        current = data["sessions"][0]["current"]
        current["limits"][0]["used_percent"] = 90
        p["tokens"] = self.render(data)
        for value in (60, 40):
            current["limits"][0]["used_percent"] = value
            expected = self.render(data)
            with patch.object(app, "call") as call:
                app.publish("unused", {p["pane_id"]: expected}, {"panes": [p]})
                for invocation in call.call_args_list:
                    p["tokens"].update(invocation.args[2]["tokens"])
            self.assertEqual(p["tokens"], expected)
        expected = self.render(data, now=NOW + 121)
        with patch.object(app, "call") as call:
            app.publish("unused", {p["pane_id"]: expected}, {"panes": [p]})
            for invocation in call.call_args_list:
                p["tokens"].update(invocation.args[2]["tokens"])
        self.assertEqual(self.visible(p["tokens"]), [("usage stale", None)])

    def test_clear_batches_respect_api_limit_and_preserve_other_tokens(self):
        p = pane(agent=None, tokens={**dict.fromkeys(app.TOKENS, "old"), "other": "preserved"})
        with patch.object(app, "call") as call:
            app.publish("unused", {}, {"panes": [p]})
            patches = [invocation.args[2]["tokens"] for invocation in call.call_args_list]
        self.assertTrue(all(len(patch) <= 16 for patch in patches))
        combined = {key: value for patch in patches for key, value in patch.items()}
        self.assertEqual(combined, dict.fromkeys(app.TOKENS))
        self.assertNotIn("other", combined)


class InstallTests(unittest.TestCase):
    def test_sidebar_has_two_rows_and_valid_token_limit(self):
        doc = tomllib.loads(install.sidebar())
        rows = doc["ui"]["sidebar"]["agents"]["rows"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(row) <= 16 for row in rows))

    def test_radar_handover_preserves_other_configuration(self):
        original = '[ui]\nsidebar_width = 55\n' + install.RADAR_START + '\n[ui.sidebar.agents]\nrows = [["agent"]]\n' + install.RADAR_END + '\n[theme]\nname = "terminal"\n'
        with self.assertRaises(ValueError):
            install.candidate(original)
        doc = tomllib.loads(install.candidate(original, True))
        self.assertEqual(doc["ui"]["sidebar_width"], 55)
        self.assertEqual(doc["theme"], {"name": "terminal"})

    def test_unowned_sidebar_is_not_overwritten(self):
        for title in ('[ui.sidebar.agents]', '[ui."sidebar".agents]'):
            with self.assertRaises(ValueError):
                install.candidate(title + '\nrows = [["agent"]]\n', True)

    def test_install_is_idempotent(self):
        once = install.candidate('[ui]\nsidebar_width = 55\n')
        self.assertEqual(install.candidate(once), once)

    def test_broken_managed_block_is_refused(self):
        with self.assertRaises(ValueError):
            install.candidate(install.START + "\n")


class SocketTests(unittest.TestCase):
    def test_real_socket_round_trip_and_metadata_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            endpoint = str(Path(directory) / "herdr.sock")
            server = socket.socket(socket.AF_UNIX)
            server.bind(endpoint)
            server.listen()
            calls = []

            def serve():
                for _ in range(2):
                    client, _ = server.accept()
                    with client, client.makefile("rb") as stream:
                        request = json.loads(stream.readline())
                        calls.append(request)
                        result = {"snapshot": {"panes": [pane()]}} if request["method"] == "session.snapshot" else {}
                        client.sendall((json.dumps({"id": request["id"], "result": result}) + "\n").encode())

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            snapshot = app.call(endpoint, "session.snapshot")["snapshot"]
            rows = app.Renderer({}).render(snapshot, usage(), NOW)
            app.publish(endpoint, rows, snapshot)
            thread.join(3)
            server.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(calls[1]["method"], "pane.report_metadata")
            params = calls[1]["params"]
            self.assertEqual(params["source"], "plugin:jermen.status-indicator")
            self.assertEqual(set(params), {"source", "pane_id", "tokens"})
            self.assertLessEqual(len(params["tokens"]), 16)


if __name__ == "__main__":
    unittest.main()
