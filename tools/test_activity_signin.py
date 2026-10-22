"""Offline regressions for sign-in records supplied by the login snapshot."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch

from backend import tasks
from backend.game_client import GameClient, GameError, GameMessage


ACTIVITY = "WeekSignInHandler.SignIn"
CALENDAR = "MonthSignInHandler.SignIn"
STATUS = "ServerStatusHandler.Query"
MAIL = "MailHandler.QueryNewestMails"


def record(activity="ActivitySignInH610", day=0):
    return {
        "_id": {"$oid": "record-" + activity}, "ActivityID": activity, "SignInDay": day,
        "CreateTime": {"$date": 0}, "LastLoginTime": {"$date": 0},
        "LastSignInTime": {"$date": -62135596800000},
        "LastContinuousSignInTime": {"$date": -62135596800000},
    }


class ActivitySigninTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("httpx.Client.request", side_effect=AssertionError("Real HTTP forbidden")))
        self.now_ms = int(datetime(2026, 10, 14, 2, tzinfo=timezone.utc).timestamp() * 1000)
        self.enterContext(patch.object(tasks.time, "time", side_effect=lambda: self.now_ms / 1000))
        self.client = Mock(spec=GameClient)
        self.client.account_state = {
            "Info": {"LV": 1}, "MailContainer": {"Mails": []}, "Utc": str(self.now_ms // 1000),
            "WeekSignInDataContainer": {"WeekSignInDataList": []},
        }
        self.client._auth_data.side_effect = lambda extra=None: {
            "AID": "test-aid", "SessionID": "test-session", **(extra or {})}
        self.client.call.side_effect = self.reply
        self.responses = {ACTIVITY: [], MAIL: []}
        self.calls = []
        self.runtime = {"loginRevision": 1, "stateSyncedAtMs": self.now_ms}

    def reply(self, route, data):
        self.calls.append((route, deepcopy(data)))
        queue = self.responses.get(route, [])
        if queue:
            response = queue.pop(0)
        elif route == CALENDAR:
            response = {"NowDay": 9}
        elif route == STATUS:
            response = {"Event": "None", "HasMail": False}
        elif route == MAIL:
            response = {"Mails": []}
        elif route == ACTIVITY:
            response = next(r for r in self.records if tasks._oid(r["_id"]) == data["_id"])
        else:
            raise AssertionError(f"Unexpected route: {route}")
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)

    @property
    def records(self):
        return self.client.account_state["WeekSignInDataContainer"]["WeekSignInDataList"]

    def with_records(self, *rows):
        self.client.account_state["WeekSignInDataContainer"]["WeekSignInDataList"] = list(rows)

    def activity_calls(self):
        return [data for route, data in self.calls if route == ACTIVITY]

    def run_signin(self):
        return tasks.task_month_signin(self.client, runtime=self.runtime)

    def test_new_activity_has_no_metadata_or_level_dependency(self):
        row = record()
        self.with_records(row)
        self.responses[ACTIVITY] = [{**row, "SignInDay": 1}]
        result = self.run_signin()
        self.assertTrue(result["ok"], result["detail"])
        self.assertEqual(self.records[0]["SignInDay"], 1)
        self.assertEqual(self.activity_calls(), [{
            "AID": "test-aid", "SessionID": "test-session", "_id": "record-ActivitySignInH610",
            "ActivityID": "ActivitySignInH610", "SignInDay": 0,
            "CreateTime": "1970/1/1 0:0:0", "LastLoginTime": "1970/1/1 0:0:0",
            "LastSignInTime": "0001/1/1 0:0:0", "LastContinuousSignInTime": "0001/1/1 0:0:0",
        }])
        self.assertTrue(result["raw"]["activities"][0]["rewarded"])

    def test_unchanged_response_is_success_and_same_login_never_resends(self):
        row = record(day=3)
        self.with_records(row)
        self.assertTrue(self.run_signin()["ok"])
        self.assertEqual(self.records, [row])
        self.assertTrue(self.run_signin()["skipped"])
        self.assertEqual(len(self.activity_calls()), 1)
        self.assertEqual(self.runtime["checkedRevision"], 1)

    def test_same_day_relogin_skips_completed_signins_and_next_day_claims_again(self):
        row = record(day=2)
        self.with_records(row)
        signed = {"$date": self.now_ms}
        self.responses[CALENDAR] = [{"NowDay": 9, "LastSignInTime": signed}]
        self.responses[ACTIVITY] = [{**row, "SignInDay": 3, "LastSignInTime": signed,
                                    "LastLoginTime": signed}]
        self.assertTrue(self.run_signin()["ok"])
        self.runtime["loginRevision"] = 2
        self.assertTrue(self.run_signin()["ok"])
        self.assertEqual(len(self.activity_calls()), 1)
        self.assertEqual(sum(route == CALENDAR for route, _ in self.calls), 1)
        self.assertEqual(self.runtime["checkedRevision"], 2)
        self.now_ms += 86_400_000
        self.runtime["loginRevision"] = 3
        self.assertTrue(self.run_signin()["ok"])
        self.assertEqual(len(self.activity_calls()), 2)
        self.assertEqual(sum(route == CALENDAR for route, _ in self.calls), 2)

    def test_today_snapshot_skips_requests_even_when_activity_login_time_is_old(self):
        self.client.account_state["MonthSignInData"] = {
            "NowDay": 9, "LastSignInTime": {"$date": self.now_ms}}
        self.with_records({**record(day=3), "LastSignInTime": {"$date": self.now_ms}})
        self.assertTrue(self.run_signin()["ok"])
        self.assertEqual([route for route, _ in self.calls], [STATUS])
        self.assertEqual(self.runtime["calendarCheckedRevision"], 1)
        self.assertEqual(self.runtime["activityCheckedRevision"], 1)

    def test_seven_days_or_more_skip_requests_even_with_old_or_missing_dates(self):
        first, second = record("Newbie", 7), record("Returning", 8)
        second["LastSignInTime"] = None
        self.with_records(first, second)
        self.assertTrue(self.run_signin()["ok"])
        self.assertEqual(self.activity_calls(), [])
        self.assertEqual(self.records, [first, second])

    def test_finished_reward_is_complete_without_request_retries_or_cache_changes(self):
        row = record("Returning", day=6)
        self.with_records(row)
        self.responses[ACTIVITY] = [GameMessage("Reward is Finish")]
        result = self.run_signin()
        self.assertTrue(result["ok"], result["detail"])
        self.assertFalse(result["retryable"])
        self.assertTrue(result["raw"]["activities"][0]["finished"])
        self.assertEqual(self.records, [row])
        self.assertTrue(self.run_signin()["skipped"])
        self.assertEqual(len(self.activity_calls()), 1)

    def test_partial_failure_retries_only_failed_activity(self):
        first, finished, failed = record("Newbie"), record("Returning", 7), record()
        self.with_records(first, finished, failed)
        self.responses[ACTIVITY] = [first, GameError("connection interrupted"), failed]
        self.assertFalse(self.run_signin()["ok"])
        self.assertNotIn("checkedRevision", self.runtime)
        self.assertTrue(self.run_signin()["ok"])
        self.assertEqual([r["ActivityID"] for r in self.activity_calls()],
                         ["Newbie", "ActivitySignInH610", "ActivitySignInH610"])
        self.assertEqual(sum(route == CALENDAR for route, _ in self.calls), 1)
        self.assertEqual(sum(route == STATUS for route, _ in self.calls), 1)

    def test_unknown_business_errors_are_not_mistaken_for_finished_rewards(self):
        self.with_records(record())
        for message in ("Error_ServerBusy", "Error_AlreadyInvalid", "Reward is Finish: invalid"):
            with self.subTest(message=message):
                self.responses[ACTIVITY] = [GameMessage(message)]
                result = self.run_signin()
                self.assertFalse(result["ok"])
                self.assertTrue(result["retryable"])
                self.assertNotIn("activityCheckedRevision", self.runtime)

    def test_wrong_record_or_malformed_response_never_changes_cached_record(self):
        row = record(day=2)
        self.with_records(row)
        for response in ({}, [], {**row, "ActivityID": "different"},
                         {**row, "_id": {"$oid": "different"}}, {**row, "SignInDay": 1},
                         {**row, "SignInDay": True}, {**row, "LastSignInTime": None}):
            with self.subTest(response=response):
                self.responses[ACTIVITY] = [response]
                self.assertFalse(self.run_signin()["ok"])
                self.assertEqual(self.records, [row])
                self.assertNotIn("activityCheckedRevision", self.runtime)

    def test_invalid_snapshot_record_sends_no_request_but_other_activities_continue(self):
        for field, value in (("ActivityID", ""), ("ActivityID", 123), ("_id", None),
                             ("SignInDay", -1), ("SignInDay", True), ("SignInDay", "1"),
                             ("CreateTime", None)):
            with self.subTest(field=field, value=value):
                self.runtime = {"loginRevision": 1}
                self.calls.clear()
                self.with_records({**record(), field: value}, record("Newbie"))
                self.assertFalse(self.run_signin()["ok"])
                self.assertEqual([r["ActivityID"] for r in self.activity_calls()], ["Newbie"])

    def test_mail_failure_retries_mail_only_after_successful_activity(self):
        self.with_records(record())
        self.responses[MAIL] = [GameError("mail unavailable"), {"Mails": []}]
        self.assertFalse(self.run_signin()["ok"])
        self.assertTrue(self.run_signin()["ok"])
        self.assertEqual(len(self.activity_calls()), 1)
        self.assertEqual(sum(route == CALENDAR for route, _ in self.calls), 1)
        self.assertEqual(sum(route == MAIL for route, _ in self.calls), 2)

    def test_many_successful_activities_share_one_mail_query_without_claiming(self):
        first, second = record(), record("Newbie")
        self.with_records(first, second)
        self.responses[ACTIVITY] = [{**first, "SignInDay": 1}, {**second, "SignInDay": 1}]
        mails = [{"_id": {"$oid": "mail"}, "IsReceived": False}]
        self.responses[MAIL] = [{"Mails": mails}]
        self.assertTrue(self.run_signin()["ok"])
        self.assertEqual(sum(route == MAIL for route, _ in self.calls), 1)
        self.assertEqual(self.client.account_state["MailContainer"]["Mails"], mails)
        self.assertEqual({route for route, _ in self.calls}, {CALENDAR, STATUS, ACTIVITY, MAIL})

    def test_same_activity_records_update_their_own_record_and_duplicates_submit_once(self):
        first = record()
        second = {**record(), "_id": {"$oid": "other-record"}, "SignInDay": 2}
        self.with_records(first, second, deepcopy(second))
        self.responses[ACTIVITY] = [{**first, "SignInDay": 1}, {**second, "SignInDay": 3}]
        self.assertTrue(self.run_signin()["ok"])
        self.assertEqual([r["SignInDay"] for r in self.records], [1, 3, 2])
        self.assertEqual([r["_id"] for r in self.activity_calls()],
                         ["record-ActivitySignInH610", "other-record"])

    def test_empty_snapshot_completes_without_activity_requests(self):
        self.assertTrue(self.run_signin()["ok"])
        self.assertEqual(self.activity_calls(), [])
        self.assertEqual(self.runtime["activityCheckedRevision"], 1)

    def test_server_day_overrides_local_day_for_both_signins(self):
        for delta, should_request in ((86_400_000, True), (-86_400_000, False)):
            with self.subTest(delta=delta):
                self.runtime = {"loginRevision": 1, "stateSyncedAtMs": self.now_ms}
                self.calls.clear()
                server_ms = self.now_ms + delta
                signed_ms = self.now_ms if should_request else server_ms
                self.client.account_state["Utc"] = str(server_ms // 1000)
                self.client.account_state["MonthSignInData"] = {
                    "NowDay": 9, "LastSignInTime": {"$date": signed_ms}}
                self.with_records({**record(day=3), "LastSignInTime": {"$date": signed_ms}})
                self.assertTrue(self.run_signin()["ok"])
                self.assertEqual(len(self.activity_calls()), int(should_request))
                self.assertEqual(sum(route == CALENDAR for route, _ in self.calls), int(should_request))

    def test_new_login_ignores_previous_login_server_clock(self):
        self.runtime.update({"loginRevision": 2, "serverTimeLoginRevision": 1,
                             "serverTimeMs": self.now_ms - 86_400_000,
                             "serverTimeSyncedAtMs": self.now_ms})
        self.client.account_state["MonthSignInData"] = {
            "NowDay": 9, "LastSignInTime": {"$date": self.now_ms}}
        self.with_records({**record(day=3), "LastSignInTime": {"$date": self.now_ms}})
        self.assertTrue(self.run_signin()["ok"])
        self.assertEqual([route for route, _ in self.calls], [STATUS])

    def test_latest_response_corrects_game_day_for_remaining_activities(self):
        self.responses[CALENDAR] = [{"NowDay": 10, "Utc": str((self.now_ms + 86_400_000) // 1000)}]
        self.with_records({**record(day=3), "LastSignInTime": {"$date": self.now_ms}})
        self.assertTrue(self.run_signin()["ok"])
        self.assertEqual(len(self.activity_calls()), 1)

    def test_beijing_midnight_relogin_keeps_previous_game_day(self):
        signed_ms = int(datetime(2026, 10, 14, 2, tzinfo=timezone.utc).timestamp() * 1000)
        for hour, minute in ((16, 0), (16, 5), (23, 59)):
            with self.subTest(hour=hour, minute=minute):
                self.now_ms = int(datetime(2026, 10, 14, hour, minute,
                                          tzinfo=timezone.utc).timestamp() * 1000)
                self.runtime = {"loginRevision": 1, "stateSyncedAtMs": self.now_ms}
                self.calls.clear()
                self.client.account_state["Utc"] = str(self.now_ms // 1000)
                self.client.account_state["MonthSignInData"] = {
                    "NowDay": 9, "LastSignInTime": {"$date": signed_ms}}
                self.with_records({**record(day=3), "LastSignInTime": {"$date": signed_ms}})
                self.assertTrue(self.run_signin()["ok"])
                self.assertEqual([route for route, _ in self.calls], [STATUS])

    def test_beijing_eight_oclock_starts_next_game_day(self):
        signed_ms = int(datetime(2026, 10, 14, 23, 59, 59,
                                 tzinfo=timezone.utc).timestamp() * 1000)
        for elapsed_ms, should_request in ((0, False), (999, False), (1000, True)):
            with self.subTest(elapsed_ms=elapsed_ms):
                self.now_ms = signed_ms + elapsed_ms
                self.runtime = {"loginRevision": 1, "stateSyncedAtMs": self.now_ms}
                self.calls.clear()
                self.client.account_state["Utc"] = str(self.now_ms // 1000)
                self.client.account_state["MonthSignInData"] = {
                    "NowDay": 9, "LastSignInTime": {"$date": signed_ms}}
                self.with_records({**record(day=3), "LastSignInTime": {"$date": signed_ms}})
                self.assertTrue(self.run_signin()["ok"])
                self.assertEqual(len(self.activity_calls()), int(should_request))
                self.assertEqual(sum(route == CALENDAR for route, _ in self.calls),
                                 int(should_request))


if __name__ == "__main__":
    unittest.main()
