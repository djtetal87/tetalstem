// TetalStem: the composer's quick picker for the separation mode.
// It reads and writes the same "separation_quality" setting as Settings,
// so whichever one you change, the next song uses it.
const sel = document.getElementById("tetalQuality");

async function load() {
  try {
    const r = await fetch("/api/settings");
    if (!r.ok) return;
    const d = await r.json();
    if (d && d.separation_quality) sel.value = d.separation_quality;
  } catch {
    /* backend not up yet; keep the default */
  }
}

async function save(value) {
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ separation_quality: value }),
    });
  } catch {
    /* ignore: Settings shows the real value next time */
  }
  const s = document.querySelector(".set-separation-quality");
  if (s) s.value = value;
}

if (sel) {
  sel.addEventListener("change", () => save(sel.value));
  // Settings changed elsewhere: refresh when the window regains focus.
  window.addEventListener("focus", load);
  document.addEventListener("change", (e) => {
    if (e.target && e.target.classList && e.target.classList.contains("set-separation-quality")) {
      sel.value = e.target.value;
    }
  });
  load();
}
