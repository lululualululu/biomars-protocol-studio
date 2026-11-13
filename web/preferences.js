// Browser preferences are deliberately separate from service credentials.
export const defaults = Object.freeze({
  model: "qwen/qwen3.6-flash",
  reading: "standard",
  font: "sans",
  paper: "white",
  reduceMotion: false,
  cell_line: "",
  species: "",
  scope: "",
});
export function normalizePreferences(value) {
  const p = value && typeof value === "object" ? value : {};
  const text = (key, max) =>
    typeof p[key] === "string" ? p[key].slice(0, max) : "";
  return {
    model: text("model", 100) || defaults.model,
    reading: ["compact", "standard", "large"].includes(p.reading) ? p.reading : "standard",
    font: p.font === "serif" ? "serif" : "sans",
    paper: p.paper === "warm" ? "warm" : "white",
    reduceMotion: p.reduceMotion === true,
    cell_line: text("cell_line", 120),
    species: text("species", 120),
    scope: text("scope", 500),
  };
}
export function loadPreferences() {
  try {
    return normalizePreferences(
      JSON.parse(localStorage.getItem("biomars-preferences") || "{}"),
    );
  } catch {
    return { ...defaults };
  }
}
export function savePreferences(value) {
  try {
    localStorage.setItem(
      "biomars-preferences",
      JSON.stringify(normalizePreferences(value)),
    );
    return true;
  } catch {
    return false;
  }
}
