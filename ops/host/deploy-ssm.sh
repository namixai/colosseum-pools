#!/usr/bin/env bash
# Puts one commit of this repository on the pools host through AWS Systems Manager: no ssh, and no
# address of the host anywhere. The host's network is the host's own setting
# (/etc/colosseum/host.env, read by install.sh).
#
# Not usable on the pools host yet: on 5 November 2026 the instance has no role, its SSM agent is not
# registered and there is no bucket. Until that is set up, releases go through ops/host/deploy.sh.
# docs/HOSTING.md, "How the host is reached", says what to set up.
#
#   COLOSSEUM_SSM_BUCKET=<bucket> ops/host/deploy-ssm.sh <commit> <instance id>
#
# The same release as ops/host/deploy.sh: only a commit on origin/main, its tracked files as a git
# archive made here, and that commit's ops/host/install.sh run as root on the host. Only the road
# differs. The archive (2.4 MB gzipped on 3 November 2026) is far over what one Run Command carries
# (a document is capped at 64 KB), so it goes to S3 under a key of its own run, and the host fetches
# it through a presigned link: the instance's role needs no S3 permission, and only this machine's
# environment names the bucket. The host checks the archive's sha256, taken here, before it unpacks
# anything. AWS credentials and the region come from the aws CLI's own configuration.
#
# Three times bound a run. The agent has COLOSSEUM_SSM_DELIVERY_S (10 minutes) to take the command,
# or SSM itself ends it as TimedOut; the link lives that long plus curl's 5 minutes, since the host
# fetches first. The install then has COLOSSEUM_SSM_EXEC_S (25 minutes). This script waits for the
# sum and two more, so SSM ends the command before the wait does. The archive is removed once the
# command has ended, or was never sent; while it may still run, it stays and the script says where.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
usage="usage: COLOSSEUM_SSM_BUCKET=<bucket> ops/host/deploy-ssm.sh <commit> <instance id>"
sha="$(git rev-parse --verify "${1:?$usage}^{commit}")"
instance="${2:?$usage}"
bucket="${COLOSSEUM_SSM_BUCKET:?$usage}"
poll="${COLOSSEUM_SSM_POLL_S:-5}"
delivery_s="${COLOSSEUM_SSM_DELIVERY_S:-600}"
exec_s="${COLOSSEUM_SSM_EXEC_S:-1500}"
wait_s="${COLOSSEUM_SSM_WAIT_S:-$((delivery_s + exec_s + 120))}"

[[ "$instance" =~ ^i-[0-9a-f]{8,17}$ ]] || { echo "deploy-ssm: not an instance id: $instance" >&2; exit 2; }

# The same gate as deploy.sh, before anything leaves this machine.
if git merge-base --is-ancestor "$sha" origin/main 2>/dev/null; then
  echo "deploy-ssm: $sha is on origin/main"
else
  echo "deploy-ssm: $sha is NOT on origin/main; nothing was sent. Fetch, or merge it first." >&2
  exit 1
fi

work="$(mktemp -d)"
# A key of this run: two runs of one commit, to two hosts or one after the other, never share an
# object, so one's cleanup can't take the archive the other is still fetching.
object="s3://$bucket/colosseum-pools/$sha-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.gz"
uploaded="" sending="" command_id="" ended=""
cleanup() {
  rm -rf "$work"
  [ -n "$uploaded" ] || return 0
  # Gone only when no command can fetch it: none was ever asked for, or the one sent has ended. A
  # send-command that failed after SSM took the request leaves no id, and its command may run.
  if [ -z "$sending" ] || [ -n "$ended" ]; then
    aws s3 rm --only-show-errors "$object" || true
  else
    echo "deploy-ssm: the archive stays at $object, because command" \
      "${command_id:-(no id: send-command failed, SSM may have taken it)} may still fetch it." \
      "Remove it once the command has ended." >&2
  fi
}
trap cleanup EXIT

git archive --format=tar.gz -o "$work/release.tar.gz" "$sha"
if command -v sha256sum >/dev/null 2>&1; then
  digest="$(sha256sum "$work/release.tar.gz" | cut -d' ' -f1)"
else
  digest="$(shasum -a 256 "$work/release.tar.gz" | cut -d' ' -f1)"
fi
aws s3 cp --only-show-errors "$work/release.tar.gz" "$object"
uploaded=1
# The wait starts here, before the link exists, so nothing below runs on time the link doesn't have.
deadline=$((SECONDS + wait_s))
url="$(aws s3 presign "$object" --expires-in "$((delivery_s + 300))")"

# What runs on the host, as root: fetch, check, unpack where deploy.sh unpacks, install. One bash
# heredoc, so the shell the agent starts doesn't matter.
SHA="$sha" DIGEST="$digest" URL="$url" EXEC_S="$exec_s" python3 - "$work/parameters.json" <<'PY'
import json, os, shlex, sys

sha, digest, url = os.environ["SHA"], os.environ["DIGEST"], os.environ["URL"]
upload = f"/opt/colosseum-pools/releases/.{sha}.upload"
release = [
    'archive="$(mktemp)"',
    "trap 'rm -f \"$archive\"' EXIT",
    f'curl -fsS --max-time 300 -o "$archive" {shlex.quote(url)}',
    f'echo "{digest}  $archive" | sha256sum -c --quiet -',
    f"rm -rf {upload}",
    f"install -d -m 0755 {upload}",
    f'tar -xz --no-same-owner -C {upload} -f "$archive"',
    f"bash {upload}/ops/host/install.sh {sha}",
]
commands = ["bash -euo pipefail <<'RELEASE'", *release, "RELEASE"]
with open(sys.argv[1], "w") as out:
    json.dump({"commands": commands, "executionTimeout": [os.environ["EXEC_S"]]}, out, indent=1)
PY

sending=1
command_id="$(aws ssm send-command --instance-ids "$instance" --document-name AWS-RunShellScript \
  --comment "colosseum-pools ${sha:0:12}" --parameters "file://$work/parameters.json" \
  --timeout-seconds "$delivery_s" --query Command.CommandId --output text)"
echo "deploy-ssm: command $command_id sent to $instance"

ask() {  # <field of the invocation>
  aws ssm get-command-invocation --command-id "$command_id" --instance-id "$instance" \
    --query "$1" --output text
}
errors=0
while :; do
  if status="$(ask Status 2>"$work/ask.err")"; then
    errors=0
  elif grep -q InvocationDoesNotExist "$work/ask.err"; then
    status=Pending  # right after the send the invocation may not exist yet
  else
    # Anything else (no permission to read, a bad id) does not go away by waiting.
    errors=$((errors + 1))
    if [ "$errors" -ge 3 ]; then
      echo "deploy-ssm: could not read command $command_id: $(tail -n 1 "$work/ask.err")" >&2
      exit 1
    fi
    status=unread
  fi
  # Status ends in one of these four. Why it ended (DeliveryTimedOut for an agent that never took
  # the command, Undeliverable, Terminated, ExecutionTimedOut) is in StatusDetails, read below.
  case "$status" in Success|Failed|Cancelled|TimedOut) break ;; esac
  if [ "$SECONDS" -ge "$deadline" ]; then
    aws ssm cancel-command --command-id "$command_id" --instance-ids "$instance" >/dev/null 2>&1 || true
    echo "deploy-ssm: no result after ${wait_s}s; command $command_id is still $status; asked SSM to cancel it" >&2
    exit 1
  fi
  sleep "$poll"
done
ended=1
# The verdict is the status: output that could not be read is a warning, not a failed release.
ask StandardOutputContent || echo "deploy-ssm: warning: could not read the host's stdout" >&2
ask StandardErrorContent >&2 || echo "deploy-ssm: warning: could not read the host's stderr" >&2
if [ "$status" != Success ]; then
  echo "deploy-ssm: $status ($(ask StatusDetails 2>/dev/null || echo 'no details')) on the host; command $command_id" >&2
  exit 1
fi
echo "deploy-ssm: $sha installed on $instance"
