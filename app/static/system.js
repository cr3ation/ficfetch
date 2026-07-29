"use strict";

// Copy the redirect URI straight into the clipboard for pasting into the IdP.
document.querySelectorAll("[data-copy-target]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const field = document.getElementById(btn.dataset.copyTarget);
    if (!field) return;
    try {
      await navigator.clipboard.writeText(field.value);
    } catch {
      field.select(); // clipboard API needs a secure context; selecting still helps
      return;
    }
    const original = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => (btn.textContent = original), 1500);
  });
});

// Destructive actions are plain form posts, so confirmation lives here.
document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (e) => {
    if (!confirm(form.dataset.confirm)) e.preventDefault();
  });
});

// Role selects submit their own single-field form on change.
document.querySelectorAll("select[data-autosubmit]").forEach((select) => {
  select.addEventListener("change", () => select.form.submit());
});

// Sub-options that only apply while their master checkbox is ticked. Disabling
// rather than hiding is deliberate: a disabled field submits nothing, which is
// what lets the server leave the stored values alone.
document.querySelectorAll("[data-enabled-by]").forEach((group) => {
  const master = document.getElementById(group.dataset.enabledBy);
  if (!master) return;
  const sync = () => {
    group.disabled = !master.checked;
    group.classList.toggle("opacity-40", !master.checked);
  };
  master.addEventListener("change", sync);
  sync();
});
