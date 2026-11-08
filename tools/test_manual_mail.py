"""Offline manual-mail dispatch, expiry, deduplication and automatic-state regressions."""
from copy import deepcopy
import logging
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

_data = tempfile.TemporaryDirectory(prefix="lucima-manual-mail-import-")
with patch.dict(os.environ, {"ARK_DATA_DIR": _data.name}):
    from backend import appcore, config, tasks
    from backend.game_client import GameMessage


def _cleanup():
    logger = logging.getLogger("lucima")
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).is_relative_to(Path(_data.name)):
            logger.removeHandler(handler)
            handler.close()
    _data.cleanup()


unittest.addModuleCleanup(_cleanup)
NOW = 1_793_581_200_000
MINUTE = 60_000


def mail(mid, sid="1", *, expires=NOW + 2 * 24 * 60 * MINUTE,
         received=False, sent=NOW - 3 * 24 * 60 * MINUTE):
    value = {"_id": {"$oid": mid}, "SentTime": {"$date": sent},
             "IsReceived": received,
             "DropResult": {"Items": [{"Item": {"StaticID": sid, "Count": 50}}]}}
    if expires is not None:
        value["ExpiryTime"] = {"$date": expires}
    return value


def mailbox():
    mixed = mail("mixed-energy")
    mixed["DropResult"]["Items"].append({"Item": {"StaticID": 3, "Count": 20}})
    return [mail("ordinary"), mail("near-energy", "3", expires=NOW + 10 * MINUTE),
            mail("far-energy", "3"), mixed, mail("timeless-energy", "3", expires=None),
            mail("expired", expires=NOW - 1), mail("at-expiry", "3", expires=NOW),
            mail("received", "3", received=True)]


MANUAL_IDS = ["ordinary", "near-energy", "far-energy", "mixed-energy", "timeless-energy"]


class NoAutomaticStateAccess(dict):
    def get(self, *args, **kwargs):
        raise AssertionError("Manual mail must not read automatic state")

    def __setitem__(self, key, value):
        raise AssertionError("Manual mail must not write automatic state")


class ManualMailTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(tasks.time, "time", return_value=NOW / 1000))
        self.enterContext(patch.object(tasks, "_item_names", return_value={"1": "金币", "3": "能源"}))
        self.enterContext(patch("httpx.Client.request", side_effect=AssertionError("Unexpected network request")))
        folder = self.enterContext(tempfile.TemporaryDirectory(prefix="lucima-manual-mail-case-"))
        self.enterContext(patch.object(config, "_DATA_DIR", Path(folder)))
        self.enterContext(patch.object(config, "_SETTINGS_FILE", Path(folder) / "settings.json"))

    def client(self, mails=None, *, queried=(), query_error=None, claim_error=None):
        client = Mock(account_state={"MailContainer": {"Mails": deepcopy(mailbox() if mails is None else mails)}})
        client._auth_data.side_effect = lambda extra=None: {"AID": "offline", "SessionID": "offline", **(extra or {})}
        pending = deepcopy(list(queried))

        def call(route, payload):
            if route == "MailHandler.QueryNewestMails":
                if query_error is not None:
                    raise query_error
                response = {"Mails": list(pending)}
                pending.clear()
                return response
            self.assertEqual(route, "MailHandler.ReceivedMails")
            if claim_error is not None:
                raise claim_error
            by_id = {tasks._mail_id(value): value for value in client.account_state["MailContainer"]["Mails"]}
            self.assertEqual(len(payload["MailIDList"]), len(set(payload["MailIDList"])))
            for mid in payload["MailIDList"]:
                self.assertFalse(by_id[mid]["IsReceived"], f"Duplicate receipt: {mid}")
            return {"Results": [{"Mail": deepcopy(by_id[mid])} for mid in payload["MailIDList"]]}

        client.call.side_effect = call
        return client

    def claims(self, client):
        return [entry.args[1]["MailIDList"] for entry in client.call.call_args_list
                if entry.args[0] == "MailHandler.ReceivedMails"]

    def test_manual_is_first_and_automatic_registration_is_preserved(self):
        self.assertEqual(next(iter(tasks.TASKS)), "claim_mail_all")
        first = tasks.manual_tasks()[0]
        self.assertEqual((first["id"], first["name"], first["desc"], first["ui"]),
                         ("claim_mail_all", "领取邮件", "一键领取全部未领取且未过期邮件（含能源）", ""))
        self.assertIs(tasks.TASKS["claim_mail_all"]["fn"], tasks.task_claim_mail_all)
        self.assertNotIn("claim_mail_all", [row["id"] for row in tasks.toggle_tasks()])
        self.assertEqual(tasks.TASKS["claim_mail"], {
            "name": "自动领邮件", "desc": "其他邮件立即领取；含能源的邮件按剩余有效期倒计时领取",
            "fn": tasks.task_claim_mail, "kind": "toggle", "wantsRuntime": True, "ui": "mail"})

    def test_automatic_default_keeps_far_energy_and_advances_only_its_runtime(self):
        client = self.client()
        params, runtime = {}, {"existing": "retained"}
        result = tasks.run_task(client, "claim_mail", params, runtime)
        self.assertTrue(result["ok"])
        self.assertEqual(self.claims(client), [["ordinary", "near-energy", "timeless-energy"]])
        self.assertEqual(params, {})
        self.assertEqual(runtime, {"existing": "retained", "nextCheckMs": NOW + tasks.MAIL_CHECK_INTERVAL_MS})
        self.assertIn("2 封体力邮件等待倒计时", result["detail"])
        self.assertIn("跳过 2 封已过期", result["detail"])
        self.assertFalse(next(m for m in client.account_state["MailContainer"]["Mails"]
                              if tasks._mail_id(m) == "far-energy")["IsReceived"])

    def test_automatic_configuration_and_invalid_value_fallback_are_unchanged(self):
        for window, energy_claimed in [(0, False), (5, False), (10, True),
                                       ("invalid", True), (-1, False), (None, True)]:
            with self.subTest(window=window):
                client = self.client([mail("ordinary"), mail("energy", "3", expires=NOW + 10 * MINUTE)])
                params = {"energyExpiryMinutes": window}
                before = deepcopy(params)
                tasks.run_task(client, "claim_mail", params, {})
                self.assertEqual(self.claims(client), [["ordinary", "energy"] if energy_claimed else ["ordinary"]])
                self.assertEqual(params, before)

    def test_manual_claims_all_valid_mail_and_reuses_item_summary_and_local_writeback(self):
        client = self.client()
        result = tasks.run_task(client, "claim_mail_all")
        self.assertTrue(result["ok"])
        self.assertEqual(self.claims(client), [MANUAL_IDS])
        self.assertEqual(result["detail"], "共领取 5 封邮件，跳过 2 封已过期")
        self.assertEqual(result["lines"][0], "领取了 100金币、150能源、20能源")
        for value in client.account_state["MailContainer"]["Mails"]:
            mid = tasks._mail_id(value)
            self.assertEqual(value["IsReceived"], mid in MANUAL_IDS or mid == "received")

    def test_manual_run_ignores_passed_params_and_runtime_even_with_future_deadline(self):
        for runner in (lambda client, params, runtime: tasks.run_task(client, "claim_mail_all", params, runtime),
                       lambda client, params, runtime: tasks.task_claim_mail(client, params, runtime, claim_all=True)):
            with self.subTest(runner=runner):
                client = self.client()
                params = NoAutomaticStateAccess(energyExpiryMinutes=0)
                runtime = NoAutomaticStateAccess(nextCheckMs=NOW + 30 * MINUTE)
                result = runner(client, params, runtime)
                self.assertTrue(result["ok"])
                self.assertEqual(self.claims(client), [MANUAL_IDS])
                self.assertEqual(dict(params), {"energyExpiryMinutes": 0})
                self.assertEqual(dict(runtime), {"nextCheckMs": NOW + 30 * MINUTE})

    def test_manual_api_keeps_saved_settings_toggles_and_automatic_runtime_unchanged(self):
        for enabled, window in [(False, 0), (True, 180), (True, "invalid")]:
            with self.subTest(enabled=enabled, window=window):
                account = SimpleNamespace(client=self.client(), lock=threading.RLock(),
                    params={"claim_mail": {"energyExpiryMinutes": window}},
                    toggles={"claim_mail": enabled},
                    task_runtime={"claim_mail": {"nextCheckMs": NOW + 30 * MINUTE, "existing": "retained"}})
                config.save_account("offline@example.com", toggles=account.toggles, params=account.params)
                before = deepcopy((account.params, account.toggles, account.task_runtime))
                saved = config._SETTINGS_FILE.read_bytes()
                with patch.object(appcore, "_require", return_value=account):
                    first = appcore.run("offline@example.com", ["claim_mail_all"], {"claim_mail_all": {"energyExpiryMinutes": 0}})
                    second = appcore.run("offline@example.com", ["claim_mail_all"], {})
                self.assertTrue(first["results"][0]["ok"])
                self.assertEqual(first["results"][0]["name"], "领取邮件")
                self.assertTrue(second["results"][0]["skipped"])
                self.assertEqual(self.claims(account.client), [MANUAL_IDS])
                self.assertEqual((account.params, account.toggles, account.task_runtime), before)
                self.assertEqual(config._SETTINGS_FILE.read_bytes(), saved)

    def test_manual_after_automatic_claims_deferred_energy_once_without_rescheduling(self):
        client = self.client()
        params, runtime = {"energyExpiryMinutes": 30}, {}
        tasks.run_task(client, "claim_mail", params, runtime)
        before = deepcopy(runtime)
        tasks.run_task(client, "claim_mail_all", params, runtime)
        repeated = tasks.run_task(client, "claim_mail_all", params, runtime)
        self.assertTrue(repeated["skipped"])
        self.assertEqual(self.claims(client), [["ordinary", "near-energy", "timeless-energy"],
                                               ["far-energy", "mixed-energy"]])
        self.assertEqual(runtime, before)

    def test_automatic_only_deferred_mail_keeps_retry_schedule_and_manual_can_claim_it(self):
        client = self.client([mail("energy", "3")])
        runtime = {}
        automatic = tasks.run_task(client, "claim_mail", {}, runtime)
        self.assertTrue(automatic["skipped"])
        self.assertEqual(automatic["raw"], {"expired": 0, "deferredEnergy": 1})
        self.assertEqual(runtime, {"nextCheckMs": NOW + tasks.MAIL_CHECK_INTERVAL_MS})
        self.assertEqual(self.claims(client), [])
        self.assertTrue(tasks.run_task(client, "claim_mail_all", {}, runtime)["ok"])
        self.assertTrue(tasks.run_task(client, "claim_mail", {}, runtime)["skipped"])
        self.assertEqual(self.claims(client), [["energy"]])

    def test_query_merges_new_mail_deduplicates_ids_and_keeps_oldest_cursor(self):
        oldest = mail("oldest", received=True, sent=NOW - 7 * 24 * 60 * MINUTE)
        client = self.client([mail("cached"), oldest],
                             queried=[mail("cached"), mail("new-energy", "3")])
        tasks.run_task(client, "claim_mail_all")
        self.assertEqual(client.call.call_args_list[0].args, ("MailHandler.QueryNewestMails",
                         {"AID": "offline", "SessionID": "offline", "MailID": "oldest"}))
        self.assertEqual(self.claims(client), [["cached", "new-energy"]])
        self.assertTrue(tasks.run_task(client, "claim_mail_all")["skipped"])
        self.assertEqual(len(self.claims(client)), 1)

    def test_query_game_message_falls_back_to_cached_mail(self):
        client = self.client(query_error=GameMessage("Offline query rejected"))
        self.assertTrue(tasks.run_task(client, "claim_mail_all")["ok"])
        self.assertEqual(self.claims(client), [MANUAL_IDS])

    def test_no_valid_mail_sends_no_receipt_and_failed_receipt_keeps_mail_unreceived(self):
        for values in ([], [mail("expired", expires=NOW), mail("received", received=True)]):
            with self.subTest(values=values):
                client = self.client(values)
                self.assertTrue(tasks.run_task(client, "claim_mail_all")["skipped"])
                self.assertEqual(self.claims(client), [])
        client = self.client([mail("energy", "3")], claim_error=GameMessage("Receipt rejected"))
        result = tasks.run_task(client, "claim_mail_all")
        self.assertFalse(result["ok"])
        self.assertEqual(result["detail"], "领取失败：Receipt rejected")
        self.assertFalse(client.account_state["MailContainer"]["Mails"][0]["IsReceived"])


if __name__ == "__main__":
    unittest.main()
