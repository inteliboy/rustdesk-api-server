let isAdmin = false;

async function loadTags() {
  const tags = await api("/api/v1/tags");
  const listEl = document.getElementById("tag-list");
  const emptyEl = document.getElementById("empty-state");
  if (tags.length === 0) {
    listEl.innerHTML = "";
    emptyEl.classList.remove("hidden");
    return;
  }
  emptyEl.classList.add("hidden");
  listEl.innerHTML = tags
    .map(
      (t) => `<span class="badge" style="background:${safeColor(t.color)}22;color:${safeColor(t.color)}">
        <span class="badge-dot" style="background:${safeColor(t.color)}"></span>
        <a href="/devices?tag_id=${t.id}" class="text-link hover:underline">${escapeHtml(t.name)}</a>
        ${isAdmin ? `<button data-id="${t.id}" class="delete-tag ml-1 opacity-60 hover:opacity-100">&times;</button>` : ""}
      </span>`
    )
    .join("");

  document.querySelectorAll(".delete-tag").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm(t("Delete this tag? It will be removed from every device."))) return;
      try {
        await api(`/api/v1/tags/${btn.dataset.id}`, { method: "DELETE" });
        loadTags();
      } catch (err) {
        toast(err.message, "error");
      }
    })
  );
}

(async () => {
  const user = await requireAuth();
  if (!user) return;
  isAdmin = user.is_admin;
  renderNav("tags", user);
  await loadTags();
})();

document.getElementById("new-tag-btn").addEventListener("click", () => {
  document.getElementById("new-tag-form").classList.toggle("hidden");
});

document.getElementById("nt-create").addEventListener("click", async () => {
  const name = document.getElementById("nt-name").value;
  const color = document.getElementById("nt-color").value;
  if (!name) {
    toast("Name is required.", "error");
    return;
  }
  try {
    await api("/api/v1/tags", { method: "POST", body: JSON.stringify({ name, color }) });
    document.getElementById("nt-name").value = "";
    document.getElementById("new-tag-form").classList.add("hidden");
    toast("Tag created.", "success");
    loadTags();
  } catch (err) {
    toast(err.message, "error");
  }
});
