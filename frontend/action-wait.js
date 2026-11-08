/* Shared foreground-action feedback. Background polling never creates a wait. */
(() => {
  const wrappers = new WeakMap(), events = new WeakMap(), locks = new WeakMap(), active = new Set();
  const controls = 'button, [role="button"], [role="switch"], input[type="submit"], input[type="checkbox"], input[type="radio"]';
  let current = null, notice = null;

  function blocked(node) {
    if (locks.has(node)) return true;
    return [...active].some(record => !record.released && record.pending && record.control === node
      && !node.classList.contains('task-stop-button'));
  }

  function renderNotice() {
    const count = [...active].filter(record => !record.released).length;
    if (!notice && count) {
      notice = document.createElement('div');
      notice.id = 'actionWaitStatus';
      notice.className = 'action-wait-status';
      notice.setAttribute('role', 'status');
      notice.setAttribute('aria-live', 'polite');
      notice.innerHTML = '<span class="action-wait-spinner" aria-hidden="true"></span><span>正在处理，请稍候…</span>';
      document.body.append(notice);
    }
    if (notice) notice.hidden = !count;
  }

  // Keep the renderer's intended disabled state while forcing the native
  // control disabled. A final paint may leave a sold-out/disconnected button
  // disabled; finishing a request must not enable it accidentally.
  function lock(record, node) {
    if (locks.has(node)) return;
    const oldBusy = node.getAttribute('aria-busy');
    const own = Object.getOwnPropertyDescriptor(node, 'disabled');
    let proto = Object.getPrototypeOf(node), descriptor;
    while (proto && !descriptor) {
      descriptor = Object.getOwnPropertyDescriptor(proto, 'disabled');
      proto = Object.getPrototypeOf(proto);
    }
    let desired = node.disabled;
    const get = descriptor?.get && (() => descriptor.get.call(node));
    if (get && !own) {
      Object.defineProperty(node, 'disabled', { configurable: true, get,
        set(value) { desired = Boolean(value); descriptor.set.call(node, true); } });
      descriptor.set.call(node, true);
    }
    // Positioned icon buttons must keep their original anchor while waiting.
    if (getComputedStyle(node).position === 'static') node.setAttribute('data-action-wait-position', '');
    node.setAttribute('data-action-waiting', '');
    node.setAttribute('aria-busy', 'true');
    const release = () => {
      if (locks.get(node) !== record) return;
      locks.delete(node);
      if (get && !own && Object.getOwnPropertyDescriptor(node, 'disabled')?.get === get) {
        delete node.disabled;
        descriptor.set.call(node, desired);
      }
      node.removeAttribute('data-action-waiting');
      node.removeAttribute('data-action-wait-position');
      if (node.getAttribute('aria-busy') === 'true') {
        if (oldBusy == null) node.removeAttribute('aria-busy');
        else node.setAttribute('aria-busy', oldBusy);
      }
    };
    locks.set(node, record);
    record.unlock.push(release);
  }

  function identity(node) {
    if (node.id) return () => document.getElementById(node.id);
    const root = node.parentElement?.closest('[id]');
    // Dynamic shelves and equipment lists replace their buttons during paint.
    // Follow explicit item identity, never a row index or the button label.
    const entries = Object.entries(node.dataset).filter(([key]) =>
      !['actionWaiting', 'actionWaitPosition', 'bound', 'rendered'].includes(key));
    if (!root || !entries.length) return () => node;
    return () => [...(document.getElementById(root.id)?.querySelectorAll(controls) || [])]
      .find(candidate => entries.every(([key, value]) => candidate.dataset[key] === value));
  }

  function refreshLocks(record) {
    if (record.released || !record.pending) return;
    const node = record.find();
    // A batch start button becomes the stop button immediately. Keep stopping
    // available even while the long-running request is still awaiting a reply.
    if (!node || (node.classList.contains('task-stop-button') && !node.disabled)) return;
    lock(record, node);
  }

  function release(record) {
    record.unlock.splice(0).forEach(unlock => unlock());
    if (locks.get(record.control) === record) locks.delete(record.control);
  }

  function add(record, promise) {
    if (!promise || typeof promise.then !== 'function' || record.promises.has(promise)) return;
    record.promises.add(promise);
    record.pending += 1;
    active.add(record);
    // Wait until all synchronous click listeners have run; existing validation
    // and disabled checks must see their own state during the initial click.
    queueMicrotask(() => { refreshLocks(record); renderNotice(); });
    const finish = () => {
      record.pending -= 1;
      if (record.pending) return;
      // Let the handler's catch/finally and response rendering finish first.
      queueMicrotask(() => {
        if (record.pending) return;
        release(record);
        active.delete(record);
        renderNotice();
      });
    };
    Promise.resolve(promise).then(finish, finish);
  }

  function wrap(handler) {
    if (typeof handler !== 'function') return handler;
    if (wrappers.has(handler)) return wrappers.get(handler);
    const wrapped = function(event, ...args) {
      const node = event?.target?.closest?.(controls)
        || (this?.matches?.(controls) ? this : null);
      if (!node) return handler.call(this, event, ...args);
      let record = event && events.get(event);
      if (!record && (blocked(node) || node.disabled || node.closest('[inert]'))) {
        event?.preventDefault(); event?.stopImmediatePropagation();
        return;
      }
      if (!record) {
        record = { control: node, find: identity(node), promises: new WeakSet(), pending: 0, unlock: [], released: false };
        if (event) events.set(event, record);
      }
      const previous = current;
      current = record;
      try {
        const result = handler.call(this, event, ...args);
        add(record, result);
        return result;
      } finally { current = previous; }
    };
    wrappers.set(handler, wrapped);
    wrappers.set(wrapped, wrapped);
    return wrapped;
  }

  // Capture fire-and-forget API calls made synchronously by an action as well
  // as the complete returned promise of async/await handlers.
  function track(promise) {
    if (current) add(current, promise);
    return promise;
  }

  document.addEventListener('click', event => {
    const node = event.target.closest?.(controls);
    if (node && blocked(node)) {
      event.preventDefault(); event.stopImmediatePropagation();
    }
  }, true);
  new MutationObserver(() => { for (const record of active) refreshLocks(record); })
    .observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener('lucima:account-change', () => {
    for (const record of active) { record.released = true; release(record); }
    renderNotice();
  });
  window.LucimaActionWait = Object.freeze({ wrap, track });
})();
