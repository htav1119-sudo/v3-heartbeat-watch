import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

spec=importlib.util.spec_from_file_location("heartbeat_watch",Path(__file__).with_name("heartbeat_watch.py"))
watch=importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)
START=dt.datetime(2026,11,27,12,tzinfo=dt.timezone.utc)


class Heartbeat(unittest.TestCase):
    def test_delayed_poll_emits_once_then_catches_reminder(self):
        notices=[]
        state=watch.check(watch.INITIAL,START,START+dt.timedelta(minutes=300),notices.append)
        self.assertEqual((len(notices),state["stage"]),(1,1))
        state=watch.check(state,START,START+dt.timedelta(minutes=330),notices.append)
        self.assertEqual(len(notices),1)
        state=watch.check(state,START,START+dt.timedelta(minutes=470),notices.append)
        self.assertEqual((len(notices),state["stage"]),(2,2))
        watch.check(state,START,START+dt.timedelta(minutes=900),notices.append)
        self.assertEqual(len(notices),2)

    def test_first_overdue_read_beyond_both_windows_one_notice(self):
        notices=[]
        state=watch.check(watch.INITIAL,START,START+dt.timedelta(hours=8),notices.append)
        self.assertEqual((state["stage"],len(notices)),(2,1))
        state=watch.check(state,START+dt.timedelta(hours=8),START+dt.timedelta(hours=8),notices.append)
        self.assertEqual(state,watch.INITIAL)
        watch.check(state,START+dt.timedelta(hours=8),START+dt.timedelta(hours=9),notices.append)
        self.assertEqual(len(notices),2)

    def test_unavailable_and_future_data(self):
        notices=[]
        state=watch.check(watch.INITIAL,None,START,notices.append)
        watch.check(state,START+dt.timedelta(hours=8),START,notices.append)
        self.assertEqual(len(notices),1)
        self.assertIn("unknown",notices[0])
        self.assertEqual(watch.check(state,START,START,notices.append),watch.INITIAL)

    def test_delivery_failure_does_not_consume_episode(self):
        old=watch.INITIAL.copy()
        with self.assertRaises(ValueError):
            watch.check(old,None,START,lambda _: (_ for _ in ()).throw(ValueError("offline")))
        notices=[]
        watch.check(old,None,START,notices.append)
        self.assertEqual(len(notices),1)

    def test_loads_prior_artifact_only_from_trusted_workflow(self):
        q={"head_branch":"main","head_repository_id":123,"head_sha":"abc","id":22}
        records=[dict(id=11,expired=False,workflow_run=dict(q,head_repository_id=999)),
                 dict(id=12,expired=False,workflow_run=q)]
        state={"schema":1,"episode":"source-unavailable","stage":1}
        archive=io.BytesIO()
        with zipfile.ZipFile(archive,"w") as z:
            z.writestr("heartbeat-state.json",json.dumps(state))
        def api(path,*args):
            if path.startswith("/actions/artifacts?"):return json.dumps({"artifacts":records}).encode()
            if path=="/actions/runs/22":return json.dumps(dict(path=".github/workflows/watch.yml",event="schedule",head_sha="abc")).encode()
            if path=="/actions/artifacts/12/zip":return archive.getvalue()
            self.fail(path)
        with patch.object(watch,"github",api),patch.dict(os.environ,{"DEFAULT_BRANCH":"main","GITHUB_REPOSITORY_ID":"123"}):
            self.assertEqual(watch.previous_state(),state)


if __name__=="__main__":
    unittest.main()
