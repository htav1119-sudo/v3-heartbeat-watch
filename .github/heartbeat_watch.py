import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import zipfile

STATE = Path("heartbeat-state.json")
ARTIFACT = "heartbeat-stage-v1"
SOURCE = "https://gist.githubusercontent.com/htav1119-sudo/401cef42ef552a7efd2426f43bc3f218/raw/v3-heartbeat.txt"
INITIAL = {"schema":1,"episode":"","stage":0}


def transition(previous, timestamp, now):
    if timestamp is None:
        episode, level, alert = "source-unavailable", 1, "Heartbeat source unavailable. Home Mac status is unknown."
    else:
        age = int((now - timestamp).total_seconds() // 60)
        if age < -5:
            return transition(previous, None, now)
        if age < 30:
            return INITIAL.copy(), None
        episode = hashlib.sha256(timestamp.isoformat().encode()).hexdigest()
        level = 2 if age >= 360 else 1
        alert = f"Home Mac heartbeat is {age} minutes old. Check the Mac and its network."
    if previous["episode"] == episode and previous["stage"] >= level:
        return previous, None
    return {"schema":1,"episode":episode,"stage":level}, alert


class StripExternalAuthorization(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        out = super().redirect_request(req, fp, code, msg, headers, newurl)
        if out and urllib.parse.urlsplit(out.full_url).scheme!="https":
            raise ValueError("Insecure redirect blocked")
        if out and urllib.parse.urlsplit(out.full_url).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            out.remove_header("Authorization")
        return out


def github(path, limit=1_000_000):
    repo = os.environ["GITHUB_REPOSITORY"]
    url = "https://api.github.com/repos/" + repo + path
    h = {"Authorization":"Bearer "+os.environ["GITHUB_TOKEN"],"Accept":"application/vnd.github+json",
         "User-Agent":"v3-heartbeat-watcher"}
    req = urllib.request.Request(url,headers=h)
    with urllib.request.build_opener(StripExternalAuthorization()).open(req,timeout=12) as r:
        data = r.read(limit+1)
    if len(data) > limit:
        raise ValueError("Oversized heartbeat state response")
    return data


def previous_state():
    # Only scheduled/dispatched executions of the same committed workflow on the default
    # branch may provide a state artifact. Ignore artifacts produced in other branches.
    page = 1
    while page <= 5:
        data=json.loads(github(f"/actions/artifacts?name={ARTIFACT}&per_page=100&page={page}"))
        for a in data["artifacts"]:
            run=a.get("workflow_run") or {}
            if (a["expired"] or run.get("head_branch")!=os.environ["DEFAULT_BRANCH"]
                    or run.get("head_repository_id")!=int(os.environ["GITHUB_REPOSITORY_ID"])):
                continue
            # Fetch the individual run to confirm the exact producer/repository and trigger.
            meta=json.loads(github(f"/actions/runs/{int(run['id'])}"))
            if (meta.get("path")!=".github/workflows/watch.yml" or meta.get("event") not in ["schedule","workflow_dispatch"]
                    or meta.get("head_sha")!=run.get("head_sha")):
                continue
            with zipfile.ZipFile(io.BytesIO(github(f"/actions/artifacts/{int(a['id'])}/zip",65_536))) as arc:
                info=arc.getinfo(STATE.name)
                if info.file_size>4096:
                    raise ValueError("Oversized heartbeat state document")
                state=json.loads(arc.read(info))
            if (set(state)!={"schema","episode","stage"} or state["schema"]!=1
                    or not isinstance(state["episode"],str) or state["stage"] not in (0,1,2)):
                raise ValueError("Malformed previous heartbeat state")
            return state
        if len(data["artifacts"])<100:
            return INITIAL.copy()
        page+=1
    raise ValueError("Could not establish a trusted previous state")


def receive(now):
    try:
        with urllib.request.urlopen(SOURCE+"?n="+str(int(now.timestamp())),timeout=8) as r:
            timestamp = dt.datetime.fromisoformat(r.read(150).decode().strip().replace("Z","+00:00"))
        if timestamp.tzinfo is None:
            return None
        return timestamp.astimezone(dt.timezone.utc)
    except (ValueError,OSError,urllib.error.URLError):
        return None


def send(text):
    topic=os.environ.get("NTFY_TOPIC")
    if not topic:
        raise ValueError("Owner notification destination unavailable")
    request=urllib.request.Request("https://ntfy.sh/"+urllib.parse.quote(topic,safe=""),data=text.encode(),
                                   headers={"Title":"V3 ALERT - heartbeat", "Priority":"high"})
    try:
        with urllib.request.urlopen(request,timeout=12) as response:
            if not 200<=response.status<300:
                raise ValueError("Notification service rejected heartbeat alert")
    except (OSError,urllib.error.URLError):
        raise ValueError("Heartbeat alert delivery failed") from None


def check(previous, timestamp, now, deliver):
    state, message = transition(previous, timestamp, now)
    if message is not None:
        deliver(message)
    return state


def main():
    now=dt.datetime.now(dt.timezone.utc)
    past=previous_state()
    STATE.write_text(json.dumps(past))
    latest=check(past, receive(now), now, send)
    temp=STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(latest))
    temp.replace(STATE)
    print("Heartbeat stage", latest["stage"])
    if latest["stage"]:
        raise SystemExit("Active heartbeat incident; last delivered stage is preserved")


if __name__=="__main__":
    main()
