let editingId = null;
let books = [];
let currentGuid = null;

const currentBook = () => books.find((b) => b.guid === currentGuid) || books[0];
const $ = (id) => document.getElementById(id);

function show(id, visible) {
  $(id).classList.toggle("hidden", !visible);
}

function showForm(entry) {
  editingId = entry ? entry.id : null;
  $("ef-rustdesk-id").value = entry ? entry.rustdesk_id : "";
  $("ef-rustdesk-id").disabled = !!entry;
  $("ef-alias").value = entry ? entry.alias || "" : "";
  $("ef-hostname").value = entry ? entry.hostname || "" : "";
  $("ef-platform").value = entry ? entry.platform || "" : "";
  $("ef-tags").value = entry ? (entry.tags || []).map((t) => t.name).join(", ") : "";
  $("ef-note").value = entry ? entry.note || "" : "";
  show("entry-form", true);
}

function hideForm() {
  editingId = null;
  show("entry-form", false);
}

// Which book the page is showing is kept in ?book= so a reload stays put.
function rememberBook() {
  const url = new URL(window.location.href);
  const book = currentBook();
  if (book && !book.is_personal) url.searchParams.set("book", book.guid);
  else url.searchParams.delete("book");
  history.replaceState(null, "", url);
}

// Plain text (callers escape it for HTML): a book someone else owns is shown
// with its owner, since two people may call their books alike.
function bookLabel(b) {
  return b.is_personal || b.is_owner ? b.name : b.name + " (" + b.owner + ")";
}

function renderBookChrome() {
  const book = currentBook();
  $("book-select").innerHTML = books
    .map(
      (b) =>
        `<option value="${escapeHtml(b.guid)}"${b.guid === book.guid ? " selected" : ""}>${escapeHtml(bookLabel(b))}</option>`
    )
    .join("");

  if (book.is_personal) {
    $("book-blurb").textContent =
      "Your personal address book: what your RustDesk client has synced to this server, plus entries you add here.";
  } else if (book.is_owner) {
    $("book-blurb").textContent = `A shared address book you own. It is shared with ${book.shares.length} user${book.shares.length === 1 ? "" : "s"}.`;
  } else {
    $("book-blurb").textContent = "Shared with you by " + book.owner + " - " + addressBookRuleName(book.rule).toLowerCase() + " access.";
  }
  if (book.note) $("book-blurb").textContent += " " + book.note;

  show("add-entry-btn", book.can_write);
  show("manage-book-btn", book.is_owner && !book.is_personal);
  if (!(book.is_owner && !book.is_personal)) show("manage-panel", false);
  if (!book.can_write) hideForm();
}

function renderManagePanel() {
  const book = currentBook();
  if (!book.is_owner || book.is_personal) return;
  $("mb-name").value = book.name;
  $("mb-note").value = book.note || "";
  $("share-rows").innerHTML = book.shares.length
    ? book.shares
        .map(
          (s) => `<div class="flex items-center justify-between py-1.5 border-b border-slate-100">
            <span>${escapeHtml(s.username)} <span class="text-slate-500">- ${escapeHtml(addressBookRuleName(s.rule))}</span></span>
            <button data-user="${escapeHtml(s.user_id)}" class="unshare text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">Remove</button>
          </div>`
        )
        .join("")
    : '<p class="text-slate-500">Not shared with anyone yet.</p>';
  document.querySelectorAll(".unshare").forEach((btn) =>
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/v1/address-books/${encodeURIComponent(currentBook().guid)}/shares/${encodeURIComponent(btn.dataset.user)}`, { method: "DELETE" });
        toast("Access removed.", "success");
        await loadBooks();
        renderManagePanel();
      } catch (err) {
        toast(err.message, "error");
      }
    })
  );
}

async function loadBooks(preferGuid) {
  books = await api("/api/v1/address-books");
  const wanted = preferGuid || currentGuid || new URL(window.location.href).searchParams.get("book");
  currentGuid = (books.find((b) => b.guid === wanted) || books[0]).guid;
  renderBookChrome();
  rememberBook();
}

async function loadEntries() {
  const book = currentBook();
  const query = book.is_personal ? "" : `?book=${encodeURIComponent(book.guid)}`;
  const entries = await api(`/api/v1/address-book${query}`);
  const rowsEl = $("entry-rows");
  const emptyEl = $("empty-state");
  if (entries.length === 0) {
    rowsEl.innerHTML = "";
    emptyEl.classList.remove("hidden");
    return;
  }
  emptyEl.classList.add("hidden");
  rowsEl.innerHTML = entries
    .map(
      (e) => `<tr>
        <td class="px-4 py-3">${connectLink(e.rustdesk_id)}</td>
        <td class="px-4 py-3 whitespace-nowrap">${escapeHtml(e.alias || "-")}</td>
        <td class="px-4 py-3 whitespace-nowrap">${
          // Links to the matching device in Devices (matched by RustDesk ID, and
          // only if the viewer may open it). A peer this server has never seen has
          // no device page, so its hostname stays plain text - the ID is the
          // connect link.
          e.device_id
            ? `<a href="/devices/${encodeURIComponent(e.device_id)}" title="Open in Devices" class="text-link hover:underline">${escapeHtml(e.hostname || "View device")}</a>`
            : escapeHtml(e.hostname || "-")
        }</td>
        <td class="px-4 py-3">${escapeHtml(e.platform ? fmtPlatform(e.platform) : "-")}</td>
        <td class="px-4 py-3 space-x-1">${tagBadges(e.tags)}</td>
        <td class="px-4 py-3 max-w-xs truncate" title="${escapeHtml(e.note || "")}">${escapeHtml(e.note || "-")}</td>
        <td class="px-4 py-3">${e.has_password
          ? '<span class="badge" style="background:#16a34a22;color:#16a34a"><span class="badge-dot" style="background:#16a34a"></span>Saved</span>'
          : '<span class="text-slate-400">-</span>'}</td>
        <td class="px-4 py-3 text-slate-500 whitespace-nowrap">${fmtDate(e.updated_at)}</td>
        <td class="px-4 py-3 text-right space-x-2 whitespace-nowrap">${
          book.can_write
            ? `<button data-entry="${escapeHtml(JSON.stringify(e))}" class="edit-entry text-slate-500 hover:text-slate-900">Edit</button>
               <button data-id="${escapeHtml(e.id)}" class="delete-entry text-red-500 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300">Delete</button>`
            : ""
        }</td>
      </tr>`
    )
    .join("");

  document.querySelectorAll(".edit-entry").forEach((btn) =>
    btn.addEventListener("click", () => showForm(JSON.parse(btn.dataset.entry)))
  );
  document.querySelectorAll(".delete-entry").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm(t("Delete this address book entry?"))) return;
      try {
        await api(`/api/v1/address-book/${encodeURIComponent(btn.dataset.id)}`, { method: "DELETE" });
        toast("Entry deleted.", "success");
        await refresh();
      } catch (err) {
        toast(err.message, "error");
      }
    })
  );
}

async function refresh(preferGuid) {
  await loadBooks(preferGuid);
  await loadEntries();
}

(async () => {
  const user = await requireAuth();
  if (!user) return;
  renderNav("address-book", user);
  try {
    await refresh();
  } catch (err) {
    toast(err.message, "error");
  }
})();

$("refresh-btn").addEventListener("click", () => refresh().catch((err) => toast(err.message, "error")));
$("add-entry-btn").addEventListener("click", () => showForm(null));
$("ef-cancel").addEventListener("click", hideForm);

$("book-select").addEventListener("change", async (evt) => {
  hideForm();
  show("manage-panel", false);
  try {
    await refresh(evt.target.value);
  } catch (err) {
    toast(err.message, "error");
  }
});

// -- shared books -----------------------------------------------------
$("new-book-btn").addEventListener("click", () => {
  $("nb-name").value = "";
  $("nb-note").value = "";
  show("new-book-form", true);
  $("nb-name").focus();
});
$("nb-cancel").addEventListener("click", () => show("new-book-form", false));
$("nb-save").addEventListener("click", async () => {
  try {
    const created = await api("/api/v1/address-books", {
      method: "POST",
      body: JSON.stringify({ name: $("nb-name").value, note: $("nb-note").value || null }),
    });
    show("new-book-form", false);
    toast("Shared address book created.", "success");
    await refresh(created.guid);
    show("manage-panel", true);
    renderManagePanel();
  } catch (err) {
    toast(err.message, "error");
  }
});

$("manage-book-btn").addEventListener("click", () => {
  renderManagePanel();
  show("manage-panel", $("manage-panel").classList.contains("hidden"));
});
$("mb-close").addEventListener("click", () => show("manage-panel", false));
$("mb-save").addEventListener("click", async () => {
  try {
    await api(`/api/v1/address-books/${encodeURIComponent(currentBook().guid)}`, {
      method: "PATCH",
      body: JSON.stringify({ name: $("mb-name").value, note: $("mb-note").value || null }),
    });
    toast("Address book updated.", "success");
    await loadBooks();
  } catch (err) {
    toast(err.message, "error");
  }
});
$("mb-delete").addEventListener("click", async () => {
  const book = currentBook();
  if (!confirm(t('Delete the shared address book "{1}" and all its entries? This cannot be undone.', [book.name]))) return;
  try {
    await api(`/api/v1/address-books/${encodeURIComponent(book.guid)}`, { method: "DELETE" });
    toast("Address book deleted.", "success");
    show("manage-panel", false);
    currentGuid = null;
    await refresh();
  } catch (err) {
    toast(err.message, "error");
  }
});
$("sh-add").addEventListener("click", async () => {
  const username = $("sh-username").value.trim();
  if (!username) {
    toast("Enter a username to share with.", "error");
    return;
  }
  try {
    await api(`/api/v1/address-books/${encodeURIComponent(currentBook().guid)}/shares`, {
      method: "PUT",
      body: JSON.stringify({ username, rule: Number($("sh-rule").value) }),
    });
    $("sh-username").value = "";
    toast("Address book shared.", "success");
    await loadBooks();
    renderManagePanel();
  } catch (err) {
    toast(err.message, "error");
  }
});

// -- entries ----------------------------------------------------------
$("ef-save").addEventListener("click", async () => {
  const book = currentBook();
  const tags = $("ef-tags")
    .value.split(",")
    .map((t) => t.trim())
    .filter(Boolean);
  const body = {
    alias: $("ef-alias").value || null,
    hostname: $("ef-hostname").value || null,
    platform: $("ef-platform").value || null,
    note: $("ef-note").value || null,
    tags,
  };
  try {
    if (editingId) {
      await api(`/api/v1/address-book/${encodeURIComponent(editingId)}`, { method: "PATCH", body: JSON.stringify(body) });
      toast("Entry updated.", "success");
    } else {
      const rustdeskId = $("ef-rustdesk-id").value.trim();
      if (!rustdeskId) {
        toast("RustDesk ID is required.", "error");
        return;
      }
      body.rustdesk_id = rustdeskId;
      const query = book.is_personal ? "" : `?book=${encodeURIComponent(book.guid)}`;
      await api(`/api/v1/address-book${query}`, { method: "POST", body: JSON.stringify(body) });
      toast("Entry added.", "success");
    }
    hideForm();
    await refresh();
  } catch (err) {
    toast(err.message, "error");
  }
});
