// Decorative GPT Image assets; category labels provide the accessible meaning.
const artwork = Object.freeze({
  organoids: './assets/organoids-glass-v5-icon.png',
  cells: './assets/cells-glass-v5-icon.png',
  imaging: './assets/imaging-glass-v5-icon.png',
  assays: './assets/assays-glass-v5-icon.png',
  engineering: './assets/engineering-glass-v5-icon.png',
});
export const methodArtwork = category => Object.hasOwn(artwork, category) ? artwork[category] : artwork.cells;
