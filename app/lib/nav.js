// Page switching. Each navigation renders into a page element of its own, so a view that
// finishes loading after the user has moved on writes into a page that is no longer shown,
// and its buttons can't be pressed.

/** `mount()` puts a fresh page on screen and returns it. */
export function createNavigator(mount) {
  let current = 0;
  return async function navigate(handler, onError = () => {}) {
    const id = ++current;
    const page = mount();
    try {
      await handler(page);
    } catch (err) {
      if (id === current) onError(page, err);
    }
    return id === current;
  };
}

/** Block ranges for an event query, newest first: at most `max` windows of `size` blocks,
 *  from `latest` back to `floor`. `complete` is false when older blocks were left out. */
export function logWindows(latest, floor, size, max) {
  const windows = [];
  let end = latest;
  while (end >= floor && windows.length < max) {
    const start = Math.max(floor, end - size + 1);
    windows.push({ start, end });
    end = start - 1;
  }
  return { windows, complete: end < floor };
}

/** Every page but the terms asks the entry question until it has been answered. */
export function needsGate(hash, passed) {
  return !passed && hash !== "#/terms";
}
