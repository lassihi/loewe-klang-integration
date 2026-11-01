/**
 * Loads the cards once the Home Assistant app has been defined.
 *
 * Extra modules run before the app bundle, which replaces window.customElements
 * and HTMLElement with a scoped registry polyfill. Cards defined earlier are
 * unknown to that registry, and Lovelace reports "Custom element doesn't exist",
 * so the card classes must not even be evaluated until the app is in place.
 */
const version = new URL(import.meta.url).search;

customElements
  .whenDefined("home-assistant")
  .then(() =>
    import(new URL(`./loewe-klang-cards.js${version}`, import.meta.url).href)
  );
