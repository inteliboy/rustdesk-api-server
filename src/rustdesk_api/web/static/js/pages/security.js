const INPUT_CLASS = "border border-slate-300 rounded px-2 py-1.5 text-sm";
const PRIMARY_BUTTON = "px-4 py-2 rounded-md bg-brand-600 text-white text-sm font-medium hover:bg-brand-700";
const SECONDARY_BUTTON = "px-4 py-2 rounded-md border border-slate-300 text-sm font-medium hover:bg-slate-50";
const DANGER_BUTTON =
  "px-4 py-2 rounded-md border border-red-300 dark:border-red-800 text-red-600 dark:text-red-400 text-sm font-medium hover:bg-red-50 dark:hover:bg-red-950/40";

// Recovery codes and a fresh token are shown exactly once: the server keeps
// only hashes, so there is nothing to show again.
function codeList(codes) {
  return `<div class="grid grid-cols-2 gap-x-6 gap-y-1 font-mono text-sm my-3">${codes
    .map((code) => `<span>${escapeHtml(code)}</span>`)
    .join("")}</div>`;
}

async function copyText(text, what) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`${what} copied.`, "success");
  } catch (err) {
    toast("Could not copy - select the text and copy it by hand.", "error");
  }
}

// ---------------------------------------------------------------- two-factor

async function renderTwoFactor() {
  const box = document.getElementById("two-factor");
  const status = await api("/api/v1/auth/2fa");

  if (!status.enabled && !status.available) {
    box.innerHTML = `<p class="text-sm text-slate-500">Two-factor authentication is not available: the server
      administrator has to set <code>DATA_ENCRYPTION_KEY</code> first (see the README).</p>`;
    return;
  }

  if (!status.enabled) {
    box.innerHTML = `
      <p class="text-sm text-slate-500 mb-3">Off. With it on, signing in also needs a 6-digit code from an
        authenticator app (the RustDesk client asks for it too).</p>
      <button id="tf-start" class="${PRIMARY_BUTTON}">Set up</button>
      <div id="tf-setup" class="mt-4 hidden"></div>`;
    document.getElementById("tf-start").addEventListener("click", startSetup);
    return;
  }

  const left = status.recovery_codes_remaining;
  box.innerHTML = `
    <p class="text-sm mb-1"><span class="badge badge-online"><span class="badge-dot"></span>On</span></p>
    <p class="text-sm text-slate-500 mb-4">${left} unused recovery code${left === 1 ? "" : "s"} left.</p>
    <div class="grid sm:grid-cols-2 gap-6">
      <div>
        <h3 class="text-sm font-medium mb-2">New recovery codes</h3>
        <p class="text-xs text-slate-500 mb-2">Replaces every existing code. Needs your password and a current code.</p>
        <div class="space-y-2">
          <input id="rc-password" type="password" placeholder="Password" autocomplete="current-password" class="${INPUT_CLASS} w-full" />
          <input id="rc-code" placeholder="Code" autocomplete="one-time-code" class="${INPUT_CLASS} w-full" />
          <button id="rc-go" class="${SECONDARY_BUTTON}">Generate</button>
        </div>
      </div>
      <div>
        <h3 class="text-sm font-medium mb-2">Turn off</h3>
        <p class="text-xs text-slate-500 mb-2">Needs your password and a current code.</p>
        <div class="space-y-2">
          <input id="off-password" type="password" placeholder="Password" autocomplete="current-password" class="${INPUT_CLASS} w-full" />
          <input id="off-code" placeholder="Code" autocomplete="one-time-code" class="${INPUT_CLASS} w-full" />
          <button id="off-go" class="${DANGER_BUTTON}">Turn off</button>
        </div>
      </div>
    </div>
    <div id="tf-codes" class="mt-4 hidden"></div>`;

  document.getElementById("rc-go").addEventListener("click", async () => {
    try {
      const result = await api("/api/v1/auth/2fa/recovery-codes", {
        method: "POST",
        body: JSON.stringify({
          password: document.getElementById("rc-password").value,
          code: document.getElementById("rc-code").value,
        }),
      });
      showCodes(result.recovery_codes);
      document.getElementById("rc-password").value = "";
      document.getElementById("rc-code").value = "";
    } catch (err) {
      toast(err.message, "error");
    }
  });
  document.getElementById("off-go").addEventListener("click", async () => {
    if (!confirm("Turn off two-factor authentication?")) return;
    try {
      await api("/api/v1/auth/2fa/disable", {
        method: "POST",
        body: JSON.stringify({
          password: document.getElementById("off-password").value,
          code: document.getElementById("off-code").value,
        }),
      });
      toast("Two-factor authentication is off.", "success");
      renderTwoFactor();
    } catch (err) {
      toast(err.message, "error");
    }
  });
}

function showCodes(codes) {
  const holder = document.getElementById("tf-codes") || document.getElementById("tf-setup");
  holder.classList.remove("hidden");
  holder.innerHTML = `
    <div class="rounded-md border border-amber-300 dark:border-amber-700 p-3">
      <p class="text-sm font-medium">Save these recovery codes now.</p>
      <p class="text-xs text-slate-500">Each works once, in place of an authenticator code, when you sign in to this
        page. They are not shown again.</p>
      ${codeList(codes)}
      <button id="codes-copy" class="${SECONDARY_BUTTON}">Copy all</button>
    </div>`;
  document.getElementById("codes-copy").addEventListener("click", () => copyText(codes.join("\n"), "Recovery codes"));
}

async function startSetup() {
  const holder = document.getElementById("tf-setup");
  let setup;
  try {
    setup = await api("/api/v1/auth/2fa/setup", { method: "POST" });
  } catch (err) {
    toast(err.message, "error");
    return;
  }
  document.getElementById("tf-start").classList.add("hidden");
  holder.classList.remove("hidden");
  holder.innerHTML = `
    <ol class="list-decimal ml-5 space-y-3 text-sm">
      <li>Scan this QR code with your authenticator app:
        <img src="${escapeHtml(setup.qr_svg)}" width="200" height="200" alt="QR code to add this account to an authenticator app"
          class="my-2 rounded-md border border-slate-300" />
        Cannot scan it? Type this key into the app instead
        (or <a href="${escapeHtml(setup.otpauth_uri)}" class="text-link underline">open it in the app</a> on this device):
        <div class="font-mono break-all my-2 select-all">${escapeHtml(setup.secret)}</div>
        Choose <em>time based</em> if it asks.</li>
      <li>Enter the 6-digit code the app shows:
        <div class="flex gap-2 mt-2">
          <input id="tf-code" inputmode="numeric" autocomplete="one-time-code" maxlength="8" class="${INPUT_CLASS}" />
          <button id="tf-enable" class="${PRIMARY_BUTTON}">Turn on</button>
        </div></li>
    </ol>`;
  document.getElementById("tf-enable").addEventListener("click", async () => {
    try {
      const result = await api("/api/v1/auth/2fa/enable", {
        method: "POST",
        body: JSON.stringify({ code: document.getElementById("tf-code").value }),
      });
      const done = document.getElementById("two-factor");
      done.innerHTML = `<p class="text-sm mb-1"><span class="badge badge-online"><span class="badge-dot"></span>On</span></p>
        <div id="tf-setup"></div>
        <button id="tf-done" class="${PRIMARY_BUTTON} mt-3">I have saved them</button>`;
      showCodes(result.recovery_codes);
      document.getElementById("tf-done").addEventListener("click", renderTwoFactor);
    } catch (err) {
      toast(err.message, "error");
    }
  });
}

// -------------------------------------------------------------------- sessions

async function renderSessions() {
  const box = document.getElementById("sessions");
  const sessions = await api("/api/v1/auth/sessions");
  const rows = sessions
    .map(
      (s) => `<tr class="border-b border-slate-100 last:border-0">
        <td class="py-2 pr-4">${escapeHtml(s.kind === "client" ? "RustDesk client" : "Browser")}${
          s.current ? ` <span class="badge badge-online ml-1"><span class="badge-dot"></span>This session</span>` : ""
        }
          <span class="block text-xs text-slate-500">${escapeHtml(s.user_agent || "unknown")}</span></td>
        <td class="py-2 pr-4 text-slate-500 whitespace-nowrap">${s.ip_address ? ipLabel(s.ip_address) : "-"}</td>
        <td class="py-2 pr-4 text-slate-500 whitespace-nowrap">${fmtDate(s.created_at)}</td>
        <td class="py-2 pr-4 text-slate-500 whitespace-nowrap">${s.last_used_at ? fmtDate(s.last_used_at) : "Never"}</td>
        <td class="py-2 text-right whitespace-nowrap">${
          s.current ? "" : `<button data-id="${Number(s.id)}" class="revoke-session text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">Sign out</button>`
        }</td>
      </tr>`
    )
    .join("");
  const others = sessions.filter((s) => !s.current).length;
  box.innerHTML = `
    <table class="w-full text-sm mb-3">
      <thead class="text-left text-slate-500"><tr>
        <th class="py-1 pr-4 font-medium">Where</th><th class="py-1 pr-4 font-medium">Address</th>
        <th class="py-1 pr-4 font-medium">Signed in</th><th class="py-1 pr-4 font-medium">Last used</th><th></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
    ${others ? `<button id="revoke-others" class="${DANGER_BUTTON}">Sign out everywhere else (${others})</button>` : `<p class="text-sm text-slate-500">No other sessions.</p>`}`;

  document.querySelectorAll(".revoke-session").forEach((btn) =>
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/v1/auth/sessions/${btn.dataset.id}`, { method: "DELETE" });
        renderSessions();
      } catch (err) {
        toast(err.message, "error");
      }
    })
  );
  const all = document.getElementById("revoke-others");
  if (all) {
    all.addEventListener("click", async () => {
      if (!confirm("Sign out of every other browser and RustDesk client?")) return;
      try {
        const result = await api("/api/v1/auth/sessions/revoke-others", { method: "POST" });
        toast(`Signed out ${result.revoked} session${result.revoked === 1 ? "" : "s"}.`, "success");
        renderSessions();
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }
}

// ------------------------------------------------------------------- API keys

async function renderApiKeys(fresh) {
  const box = document.getElementById("api-keys");
  const keys = await api("/api/v1/api-keys");
  const rows = keys
    .map(
      (k) => `<tr class="border-b border-slate-100 last:border-0">
        <td class="py-2">${escapeHtml(k.label || "-")}</td>
        <td class="py-2 text-slate-500">${escapeHtml(k.scope === "full" ? "Full access" : "Read only")}</td>
        <td class="py-2 text-slate-500 whitespace-nowrap">${fmtDate(k.created_at)}</td>
        <td class="py-2 text-slate-500 whitespace-nowrap">${fmtDate(k.expires_at)}</td>
        <td class="py-2 text-slate-500 whitespace-nowrap">${k.last_used_at ? fmtDate(k.last_used_at) : "Never"}</td>
        <td class="py-2 text-right"><button data-id="${Number(k.id)}" class="revoke-key text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">Revoke</button></td>
      </tr>`
    )
    .join("");
  const freshBlock = fresh
    ? `<div class="rounded-md border border-amber-300 dark:border-amber-700 p-3 mb-4">
        <p class="text-sm font-medium">Copy this key now &mdash; it is not shown again.</p>
        <div class="font-mono text-sm break-all my-2 select-all">${escapeHtml(fresh.token)}</div>
        <div class="font-mono text-xs break-all select-all mb-2">curl -H "Authorization: Bearer ${escapeHtml(fresh.token)}" ${escapeHtml(window.location.origin)}/api/v1/devices</div>
        <button id="key-copy" class="${SECONDARY_BUTTON}">Copy key</button>
      </div>`
    : "";
  const listBlock = keys.length
    ? `<table class="w-full text-sm mb-4">
        <thead class="text-left text-slate-500"><tr>
          <th class="py-1 font-medium">Label</th><th class="py-1 font-medium">Access</th><th class="py-1 font-medium">Created</th>
          <th class="py-1 font-medium">Expires</th><th class="py-1 font-medium">Last used</th><th></th>
        </tr></thead>
        <tbody>${rows}</tbody></table>`
    : `<p class="text-sm text-slate-500 mb-4">No API keys yet.</p>`;
  box.innerHTML = `
    ${freshBlock}
    ${listBlock}
    <div class="flex flex-wrap items-end gap-3">
      <div>
        <label class="block text-xs text-slate-500 mb-1" for="key-label">Label</label>
        <input id="key-label" placeholder="e.g. nightly report" maxlength="100" class="${INPUT_CLASS}" />
      </div>
      <div>
        <label class="block text-xs text-slate-500 mb-1" for="key-scope">Access</label>
        <select id="key-scope" class="${INPUT_CLASS}">
          <option value="read" selected>Read only</option><option value="full">Full access (as you)</option>
        </select>
      </div>
      <div>
        <label class="block text-xs text-slate-500 mb-1" for="key-days">Valid for</label>
        <select id="key-days" class="${INPUT_CLASS}">
          <option value="30">30 days</option><option value="90" selected>90 days</option>
          <option value="180">180 days</option><option value="365">1 year</option>
        </select>
      </div>
      <button id="key-create" class="${PRIMARY_BUTTON}">Create key</button>
    </div>`;

  if (fresh) document.getElementById("key-copy").addEventListener("click", () => copyText(fresh.token, "Key"));
  document.querySelectorAll(".revoke-key").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm("Revoke this key? Scripts using it stop working.")) return;
      try {
        await api(`/api/v1/api-keys/${btn.dataset.id}`, { method: "DELETE" });
        renderApiKeys();
      } catch (err) {
        toast(err.message, "error");
      }
    })
  );
  document.getElementById("key-create").addEventListener("click", async () => {
    const label = document.getElementById("key-label").value.trim();
    if (!label) {
      toast("Give the key a label.", "error");
      return;
    }
    try {
      const created = await api("/api/v1/api-keys", {
        method: "POST",
        body: JSON.stringify({
          label,
          scope: document.getElementById("key-scope").value,
          days: parseInt(document.getElementById("key-days").value, 10),
        }),
      });
      renderApiKeys(created);
    } catch (err) {
      toast(err.message, "error");
    }
  });
}

// ---------------------------------------------------------- enrollment tokens

async function renderEnrollment(fresh) {
  const box = document.getElementById("enrollment");
  const tokens = await api("/api/v1/enrollment-tokens");

  const rows = tokens
    .map(
      (t) => `<tr class="border-b border-slate-100 last:border-0">
        <td class="py-2">${escapeHtml(t.label || "-")}</td>
        <td class="py-2 text-slate-500 whitespace-nowrap">${fmtDate(t.created_at)}</td>
        <td class="py-2 text-slate-500 whitespace-nowrap">${fmtDate(t.expires_at)}</td>
        <td class="py-2 text-slate-500 whitespace-nowrap">${t.last_used_at ? fmtDate(t.last_used_at) : "Never"}</td>
        <td class="py-2 text-right"><button data-id="${t.id}" class="revoke-token text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">Revoke</button></td>
      </tr>`
    )
    .join("");

  const freshBlock = fresh
    ? `<div class="rounded-md border border-amber-300 dark:border-amber-700 p-3 mb-4">
        <p class="text-sm font-medium">Copy this token now &mdash; it is not shown again.</p>
        <div class="font-mono text-sm break-all my-2 select-all">${escapeHtml(fresh.token)}</div>
        <p class="text-xs text-slate-500 mb-2">On the device (as administrator/root):</p>
        <div class="font-mono text-xs break-all select-all">rustdesk --assign --token ${escapeHtml(fresh.token)} --user_name ${escapeHtml(currentUser.username)} --address_book_name "My address book"</div>
        <button id="token-copy" class="${SECONDARY_BUTTON} mt-3">Copy token</button>
      </div>`
    : "";
  const listBlock = tokens.length
    ? `<table class="w-full text-sm mb-4">
        <thead class="text-left text-slate-500"><tr>
          <th class="py-1 font-medium">Label</th><th class="py-1 font-medium">Created</th>
          <th class="py-1 font-medium">Expires</th><th class="py-1 font-medium">Last used</th><th></th>
        </tr></thead>
        <tbody>${rows}</tbody></table>`
    : `<p class="text-sm text-slate-500 mb-4">No tokens yet.</p>`;

  box.innerHTML = `
    ${freshBlock}
    ${listBlock}
    <div class="flex flex-wrap items-end gap-3">
      <div>
        <label class="block text-xs text-slate-500 mb-1" for="tok-label">Label</label>
        <input id="tok-label" placeholder="e.g. installer script" maxlength="100" class="${INPUT_CLASS}" />
      </div>
      <div>
        <label class="block text-xs text-slate-500 mb-1" for="tok-days">Valid for</label>
        <select id="tok-days" class="${INPUT_CLASS}">
          <option value="30">30 days</option><option value="90" selected>90 days</option>
          <option value="180">180 days</option><option value="365">1 year</option>
        </select>
      </div>
      <button id="tok-create" class="${PRIMARY_BUTTON}">Create token</button>
    </div>`;

  if (fresh) {
    document.getElementById("token-copy").addEventListener("click", () => copyText(fresh.token, "Token"));
  }
  document.querySelectorAll(".revoke-token").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm("Revoke this token? Scripts using it stop working.")) return;
      try {
        await api(`/api/v1/enrollment-tokens/${btn.dataset.id}`, { method: "DELETE" });
        renderEnrollment();
      } catch (err) {
        toast(err.message, "error");
      }
    })
  );
  document.getElementById("tok-create").addEventListener("click", async () => {
    const label = document.getElementById("tok-label").value.trim();
    if (!label) {
      toast("Give the token a label.", "error");
      return;
    }
    try {
      const created = await api("/api/v1/enrollment-tokens", {
        method: "POST",
        body: JSON.stringify({ label, days: parseInt(document.getElementById("tok-days").value, 10) }),
      });
      renderEnrollment(created);
    } catch (err) {
      toast(err.message, "error");
    }
  });
}

// ------------------------------------------------------------------ password

function setupPasswordForm(user) {
  document.getElementById("pw-username").value = user.username; // lets a password manager file the new one
  const form = document.getElementById("password-form");
  const error = document.getElementById("pw-error");
  const fail = (message) => {
    error.textContent = message;
    error.classList.remove("hidden");
  };

  form.addEventListener("submit", async (evt) => {
    evt.preventDefault();
    error.classList.add("hidden");
    const current = document.getElementById("pw-current").value;
    const next = document.getElementById("pw-new").value;
    if (next !== document.getElementById("pw-confirm").value) {
      fail("The two new passwords are not the same.");
      return;
    }
    try {
      await api("/api/v1/auth/change-password", {
        method: "POST",
        body: JSON.stringify({ current_password: current, new_password: next }),
      });
    } catch (err) {
      fail(err.message);
      return;
    }
    for (const id of ["pw-current", "pw-new", "pw-confirm"]) document.getElementById(id).value = "";
    toast("Password changed. Your other sessions were signed out.", "success");
    // The lists below changed: other sessions, API keys and enrollment tokens are gone.
    try {
      await Promise.all([renderSessions(), renderApiKeys(), renderEnrollment()]);
    } catch (err) {
      toast(err.message, "error");
    }
  });
}

(async () => {
  const user = await requireAuth();
  if (!user) return;
  renderNav("security", user);
  setupPasswordForm(user);
  try {
    await Promise.all([renderTwoFactor(), renderSessions(), renderApiKeys(), renderEnrollment()]);
  } catch (err) {
    toast(err.message, "error");
  }
})();
