// Two guards the panels' buttons share, kept apart from the pages so a test can drive them without a browser.
//
// A confirmation is the second press of a button, and it belongs to what was on the screen at the first: the first
// press names the call (`key`, its calldata) and keeps what sends it; a second press with the same key sends that, a
// press with another key (the selection or the form changed in between) starts over and sends nothing.
//
// A pending guard says a submission is under way, whatever the buttons look like: a redraw that enables a button again
// does not let a second request start.

export function createConfirm() {
  let armed = null;
  return {
    /** The first press of a key arms it ("arm"); the next press of the same key sends what the first one kept ("send"). */
    press(key, run) {
      if (armed && armed.key === key) {
        const kept = armed;
        armed = null;
        return { action: "send", run: kept.run };
      }
      armed = { key, run };
      return { action: "arm" };
    },
    /** Forgets the confirmation: the screen changed. */
    reset() {
      armed = null;
    },
    get key() {
      return armed ? armed.key : null;
    },
  };
}

export function createPending() {
  let active = false;
  return {
    get active() {
      return active;
    },
    /** Runs `job` unless one is running: `{ refused: true }` then, else `{ value }`. The guard clears when the job ends, failed or not. */
    async run(job) {
      if (active) return { refused: true };
      active = true;
      try {
        return { refused: false, value: await job() };
      } finally {
        active = false;
      }
    },
  };
}
