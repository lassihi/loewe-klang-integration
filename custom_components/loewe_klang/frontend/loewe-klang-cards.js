/**
 * Lovelace cards for Loewe klang speakers, served by the integration.
 *
 *   custom:loewe-klang-player-card  cover art, transport, progress and volume
 *   custom:loewe-klang-mini-card    compact now playing and volume
 *   custom:loewe-klang-status-card  source, connections and firmware
 *
 * Titles, artists and source names come from unauthenticated LAN traffic, so
 * dynamic values are only ever written with textContent and DOM properties.
 * Never build markup from them.
 */

const PLATFORM = "loewe_klang";

// MediaPlayerEntityFeature values.
const FEATURE = {
  PAUSE: 1,
  SEEK: 2,
  VOLUME_SET: 4,
  VOLUME_MUTE: 8,
  PREVIOUS: 16,
  NEXT: 32,
  VOLUME_STEP: 1024,
  STOP: 4096,
  PLAY: 16384,
  SHUFFLE: 32768,
  REPEAT: 262144,
};

const NEXT_REPEAT = { off: "all", all: "one", one: "off" };
// How long a set volume wins over state updates that predate the command.
const VOLUME_HOLD_MS = 2000;

const BASE_STYLE = `
  :host { display: block; }
  ha-card { overflow: hidden; height: 100%; box-sizing: border-box; }
  [hidden] { display: none !important; }
  button {
    display: inline-flex; align-items: center; justify-content: center;
    width: 44px; height: 44px; flex: none; padding: 0;
    border: none; border-radius: 50%; background: none;
    color: inherit; cursor: pointer; --mdc-icon-size: 24px;
  }
  button:hover:not(:disabled) { background: rgba(127, 127, 127, 0.16); }
  button:focus-visible, input:focus-visible {
    outline: 2px solid var(--primary-color); outline-offset: 2px;
  }
  button:disabled { opacity: 0.38; cursor: default; }
  button.active { color: var(--primary-color); }
  input[type="range"] {
    flex: 1; min-width: 0; margin: 0; cursor: pointer;
    accent-color: var(--primary-color);
  }
  input[type="range"]:disabled { cursor: default; opacity: 0.5; }
  .volume {
    display: flex; align-items: center; gap: 4px; padding: 4px 8px;
    color: var(--primary-text-color);
  }
  .volume .level {
    width: 44px; flex: none; text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .missing { padding: 16px; color: var(--secondary-text-color); }
  .ellipsis { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
`;

const VOLUME_TEMPLATE = `
  <div class="volume">
    <button data-action="mute" class="mute" aria-label="Mute">
      <ha-icon icon="mdi:volume-high"></ha-icon>
    </button>
    <button data-action="volume-down" class="step" aria-label="Volume down">
      <ha-icon icon="mdi:minus"></ha-icon>
    </button>
    <input class="volume-slider" type="range" min="0" max="100" step="1"
           aria-label="Volume">
    <button data-action="volume-up" class="step" aria-label="Volume up">
      <ha-icon icon="mdi:plus"></ha-icon>
    </button>
    <span class="level"></span>
  </div>
`;

const formatTime = (value) => {
  const total = Math.max(0, Math.floor(value || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}`
    : `${minutes}:${seconds}`;
};

const currentPosition = (state) => {
  const { media_position: position, media_duration: duration } =
    state.attributes;
  if (position == null) return null;
  let value = position;
  const updated = state.attributes.media_position_updated_at;
  if (state.state === "playing" && updated) {
    value += (Date.now() - new Date(updated).getTime()) / 1000;
  }
  return duration ? Math.min(value, duration) : value;
};

const findSpeaker = (hass) =>
  Object.values(hass.entities || {}).find(
    (entry) =>
      entry.platform === PLATFORM && entry.entity_id.startsWith("media_player.")
  )?.entity_id;

// A readable backdrop for the artwork: its darkened average colour, with black
// or white text picked by luminance so the text always contrasts.
const artworkColors = (image) => {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 16;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  context.drawImage(image, 0, 0, 16, 16);
  const { data } = context.getImageData(0, 0, 16, 16);
  let [red, green, blue] = [0, 0, 0];
  for (let i = 0; i < data.length; i += 4) {
    red += data[i];
    green += data[i + 1];
    blue += data[i + 2];
  }
  const scale = 0.75 / (data.length / 4);
  [red, green, blue] = [red, green, blue].map((c) => Math.round(c * scale));
  const luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255;
  return {
    background: `rgb(${red}, ${green}, ${blue})`,
    foreground: luminance > 0.5 ? "#000" : "#fff",
  };
};

class LoeweCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._volumeHeld = 0;
    this._volumeDragging = false;
    this._seeking = false;
  }

  static getStubConfig(hass) {
    return { entity: findSpeaker(hass) || "" };
  }

  static get _schema() {
    return [
      {
        name: "entity",
        required: true,
        selector: { entity: { domain: "media_player", integration: PLATFORM } },
      },
      { name: "name", selector: { text: {} } },
    ];
  }

  static getConfigForm() {
    const labels = {
      entity: "Speaker",
      name: "Name (optional)",
      show_volume: "Show volume",
      show_progress: "Show progress",
    };
    return {
      schema: this._schema,
      computeLabel: (schema) => labels[schema.name],
    };
  }

  setConfig(config) {
    if (!config.entity || !config.entity.startsWith("media_player.")) {
      throw new Error("Choose a media_player entity");
    }
    this._config = config;
    this._build();
    this._update();
  }

  set hass(hass) {
    const previous = this._hass;
    this._hass = hass;
    if (
      !previous ||
      previous.locale !== hass.locale ||
      this._watched().some((id) => previous.states[id] !== hass.states[id])
    ) {
      this._update();
    }
  }

  connectedCallback() {
    this._timer = setInterval(() => this._tick(), 1000);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
  }

  get _state() {
    return this._hass?.states[this._config?.entity];
  }

  get _name() {
    return (
      this._config.name ||
      this._state?.attributes.friendly_name ||
      this._config.entity
    );
  }

  _supports(feature) {
    return Boolean((this._state?.attributes.supported_features || 0) & feature);
  }

  _watched() {
    return [this._config?.entity];
  }

  _tick() {}

  _$(selector) {
    return this.shadowRoot.querySelector(selector);
  }

  _render(style, body) {
    this.shadowRoot.innerHTML = `
      <style>${BASE_STYLE}${style}</style>
      <ha-card>
        <div class="content">${body}</div>
        <div class="missing" hidden></div>
      </ha-card>`;
    this.shadowRoot.addEventListener("click", (event) => {
      const control = event.target.closest("[data-action]");
      if (control && !control.disabled) this._action(control);
    });
    const slider = this._$(".volume-slider");
    if (slider) {
      slider.addEventListener("pointerdown", () => {
        this._volumeDragging = true;
      });
      slider.addEventListener("input", () => {
        this._volumeDragging = true;
        this._$(".level").textContent = `${slider.value} %`;
      });
      slider.addEventListener("change", () => {
        this._volumeDragging = false;
        this._volumeHeld = Date.now() + VOLUME_HOLD_MS;
        this._call("volume_set", { volume_level: slider.value / 100 });
      });
    }
  }

  // Shows the message instead of the card body. Returns the state when usable.
  _showState() {
    const state = this._state;
    const missing = this._$(".missing");
    missing.hidden = Boolean(state);
    this._$(".content").hidden = !state;
    if (!state) missing.textContent = `Entity not found: ${this._config.entity}`;
    return state;
  }

  _action(control) {
    const state = this._state;
    switch (control.dataset.action) {
      case "play-pause":
        if (state.state !== "playing") this._call("media_play");
        else if (this._supports(FEATURE.PAUSE)) this._call("media_pause");
        else this._call("media_stop");
        break;
      case "previous":
        this._call("media_previous_track");
        break;
      case "next":
        this._call("media_next_track");
        break;
      case "shuffle":
        this._call("shuffle_set", { shuffle: !state.attributes.shuffle });
        break;
      case "repeat":
        this._call("repeat_set", {
          repeat: NEXT_REPEAT[state.attributes.repeat] || "all",
        });
        break;
      case "mute":
        this._call("volume_mute", {
          is_volume_muted: !state.attributes.is_volume_muted,
        });
        break;
      case "volume-up":
        this._call("volume_up");
        break;
      case "volume-down":
        this._call("volume_down");
        break;
      case "more-info":
        this._moreInfo(control.dataset.entity || this._config.entity);
        break;
    }
  }

  _moreInfo(entityId) {
    this.dispatchEvent(
      new CustomEvent("hass-more-info", {
        bubbles: true,
        composed: true,
        detail: { entityId },
      })
    );
  }

  async _call(service, data = {}) {
    try {
      await this._hass.callService("media_player", service, {
        entity_id: this._config.entity,
        ...data,
      });
    } catch (err) {
      this.dispatchEvent(
        new CustomEvent("hass-notification", {
          bubbles: true,
          composed: true,
          detail: { message: err?.message || `${service} failed` },
        })
      );
    }
  }

  _setControl(selector, visible, enabled) {
    const control = this._$(selector);
    control.hidden = !visible;
    control.disabled = !enabled;
    return control;
  }

  _updatePlayPause(usable) {
    const playing = this._state.state === "playing";
    const canStop = this._supports(FEATURE.PAUSE) || this._supports(FEATURE.STOP);
    const visible = playing ? canStop : this._supports(FEATURE.PLAY);
    const control = this._setControl(".play-pause", visible, usable);
    const pauses = this._supports(FEATURE.PAUSE);
    const label = !playing ? "Play" : pauses ? "Pause" : "Stop";
    control.setAttribute("aria-label", label);
    control
      .querySelector("ha-icon")
      .setAttribute("icon", `mdi:${label.toLowerCase()}`);
  }

  _updateVolume(usable) {
    const row = this._$(".volume");
    const { volume_level: level, is_volume_muted: muted } =
      this._state.attributes;
    const canSet = this._supports(FEATURE.VOLUME_SET);
    const canStep = this._supports(FEATURE.VOLUME_STEP);
    const canMute = this._supports(FEATURE.VOLUME_MUTE);
    row.hidden =
      this._config.show_volume === false || !(canSet || canStep || canMute);
    this._setControl(".mute", canMute, usable)
      .querySelector("ha-icon")
      .setAttribute("icon", muted ? "mdi:volume-off" : "mdi:volume-high");
    this._$(".mute").setAttribute("aria-label", muted ? "Unmute" : "Mute");
    this.shadowRoot.querySelectorAll(".volume .step").forEach((control) => {
      control.hidden = !canStep;
      control.disabled = !usable;
    });
    const slider = this._$(".volume-slider");
    slider.hidden = !canSet;
    slider.disabled = !usable || level == null;
    if (this._volumeDragging || Date.now() < this._volumeHeld) return;
    const percent = level == null ? null : Math.round(level * 100);
    if (percent != null) slider.value = percent;
    this._$(".level").textContent = percent == null ? "—" : `${percent} %`;
  }

  _nowPlaying() {
    const state = this._state;
    return {
      title: state.attributes.media_title || this._hass.formatEntityState(state),
      artist:
        state.attributes.media_artist ||
        state.attributes.media_album_name ||
        "",
    };
  }
}

class LoewePlayerCard extends LoeweCard {
  static get _schema() {
    return [
      ...super._schema,
      { name: "show_volume", default: true, selector: { boolean: {} } },
      { name: "show_progress", default: true, selector: { boolean: {} } },
    ];
  }

  getCardSize() {
    return 5;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  _build() {
    if (this._$(".media")) return;
    this._render(
      `
      .media {
        position: relative; overflow: hidden; min-height: 200px;
        display: flex; flex-direction: column;
        background: var(--art-background, var(--secondary-background-color));
        color: var(--art-foreground, var(--primary-text-color));
        transition: background 0.4s;
      }
      .art {
        position: absolute; inset: 0 0 0 auto; width: 65%; height: 100%;
        object-fit: cover;
        -webkit-mask-image: linear-gradient(to right, transparent, #000 60%);
        mask-image: linear-gradient(to right, transparent, #000 60%);
      }
      .layer {
        position: relative; display: flex; flex-direction: column; flex: 1;
        padding: 8px 16px 12px;
      }
      .has-art .layer { text-shadow: 0 0 6px var(--art-background); }
      .head { display: flex; align-items: center; gap: 8px; min-height: 44px; }
      .head .name { flex: 1; min-width: 0; font-size: 14px; opacity: 0.85; }
      .head .source {
        flex: none; max-width: 40%; padding: 2px 8px; border-radius: 12px;
        font-size: 12px; background: rgba(127, 127, 127, 0.28);
      }
      .title { margin-top: 12px; font-size: 22px; line-height: 1.25; }
      .artist { margin-top: 4px; font-size: 15px; opacity: 0.8; }
      .title, .artist { max-width: 85%; }
      .controls {
        display: flex; align-items: center; gap: 4px;
        margin: auto 0 0 -10px; padding-top: 12px;
      }
      .play-pause { width: 56px; height: 56px; --mdc-icon-size: 40px; }
      .progress {
        display: flex; align-items: center; gap: 8px; margin-top: 4px;
        font-size: 12px; font-variant-numeric: tabular-nums;
      }
      .progress input { accent-color: var(--art-foreground, var(--primary-color)); }
      `,
      `
      <div class="media">
        <img class="art" alt="" hidden>
        <div class="layer">
          <div class="head">
            <span class="name ellipsis"></span>
            <span class="source ellipsis"></span>
            <button data-action="more-info" aria-label="More information">
              <ha-icon icon="mdi:dots-vertical"></ha-icon>
            </button>
          </div>
          <div class="title ellipsis"></div>
          <div class="artist ellipsis"></div>
          <div class="controls">
            <button data-action="shuffle" class="shuffle" aria-label="Shuffle">
              <ha-icon icon="mdi:shuffle"></ha-icon>
            </button>
            <button data-action="previous" class="previous" aria-label="Previous">
              <ha-icon icon="mdi:skip-previous"></ha-icon>
            </button>
            <button data-action="play-pause" class="play-pause">
              <ha-icon icon="mdi:play"></ha-icon>
            </button>
            <button data-action="next" class="next" aria-label="Next">
              <ha-icon icon="mdi:skip-next"></ha-icon>
            </button>
            <button data-action="repeat" class="repeat" aria-label="Repeat">
              <ha-icon icon="mdi:repeat"></ha-icon>
            </button>
          </div>
          <div class="progress">
            <span class="elapsed"></span>
            <input class="seek" type="range" min="0" step="1" aria-label="Seek">
            <span class="duration"></span>
          </div>
        </div>
      </div>
      ${VOLUME_TEMPLATE}
      `
    );
    const art = this._$(".art");
    art.addEventListener("load", () => this._applyArtwork(art));
    art.addEventListener("error", () => this._clearArtwork());
    const seek = this._$(".seek");
    seek.addEventListener("pointerdown", () => {
      this._seeking = true;
    });
    seek.addEventListener("input", () => {
      this._seeking = true;
      this._$(".elapsed").textContent = formatTime(seek.value);
    });
    seek.addEventListener("change", () => {
      this._seeking = false;
      this._call("media_seek", { seek_position: Number(seek.value) });
    });
  }

  _applyArtwork(art) {
    art.hidden = false;
    const media = this._$(".media");
    media.classList.add("has-art");
    try {
      const { background, foreground } = artworkColors(art);
      media.style.setProperty("--art-background", background);
      media.style.setProperty("--art-foreground", foreground);
    } catch {
      // A cross-origin image can't be read; stay on the theme colours.
      this._resetColors();
    }
  }

  _clearArtwork() {
    this._$(".art").hidden = true;
    this._$(".media").classList.remove("has-art");
    this._resetColors();
  }

  _resetColors() {
    const media = this._$(".media");
    media.style.removeProperty("--art-background");
    media.style.removeProperty("--art-foreground");
  }

  _update() {
    if (!this._hass || !this._config) return;
    const state = this._showState();
    if (!state) return;
    const usable = state.state !== "unavailable";
    const { attributes } = state;

    const picture = attributes.entity_picture || null;
    if (picture !== this._picture) {
      this._picture = picture;
      if (picture) this._$(".art").src = this._hass.hassUrl(picture);
      else this._clearArtwork();
    }

    const { title, artist } = this._nowPlaying();
    this._$(".name").textContent = this._name;
    const source = attributes.source || attributes.app_name || "";
    this._$(".source").textContent = source;
    this._$(".source").hidden = !source;
    this._$(".title").textContent = title;
    this._$(".artist").textContent = artist;

    this._updatePlayPause(usable);
    this._setControl(".previous", this._supports(FEATURE.PREVIOUS), usable);
    this._setControl(".next", this._supports(FEATURE.NEXT), usable);
    this._setControl(".shuffle", this._supports(FEATURE.SHUFFLE), usable)
      .classList.toggle("active", attributes.shuffle === true);
    const repeat = this._setControl(
      ".repeat",
      this._supports(FEATURE.REPEAT),
      usable
    );
    repeat.classList.toggle("active", (attributes.repeat || "off") !== "off");
    repeat
      .querySelector("ha-icon")
      .setAttribute(
        "icon",
        attributes.repeat === "one" ? "mdi:repeat-once" : "mdi:repeat"
      );

    this._updateProgress();
    this._updateVolume(usable);
  }

  _updateProgress() {
    const state = this._state;
    if (!state) return;
    const duration = state.attributes.media_duration;
    const position = currentPosition(state);
    const row = this._$(".progress");
    row.hidden =
      this._config.show_progress === false || !duration || position == null;
    if (row.hidden || this._seeking) return;
    const seek = this._$(".seek");
    seek.max = duration;
    seek.value = position;
    seek.disabled =
      state.state === "unavailable" || !this._supports(FEATURE.SEEK);
    this._$(".elapsed").textContent = formatTime(position);
    this._$(".duration").textContent = formatTime(duration);
  }

  _tick() {
    if (this._state?.state === "playing") this._updateProgress();
  }
}

class LoeweMiniCard extends LoeweCard {
  static get _schema() {
    return [
      ...super._schema,
      { name: "show_volume", default: true, selector: { boolean: {} } },
    ];
  }

  getCardSize() {
    return 2;
  }

  getGridOptions() {
    return { columns: 6, min_columns: 4, rows: "auto" };
  }

  _build() {
    if (this._$(".row")) return;
    this._render(
      `
      .row { display: flex; align-items: center; gap: 8px; padding: 8px 8px 0; }
      .thumb {
        width: 48px; height: 48px; flex: none; padding: 0; overflow: hidden;
        border-radius: 8px; background: var(--secondary-background-color);
        color: var(--secondary-text-color);
      }
      .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
      .text { flex: 1; min-width: 0; }
      .title { font-size: 15px; color: var(--primary-text-color); }
      .artist { font-size: 13px; color: var(--secondary-text-color); }
      .row button:not(.thumb) { color: var(--primary-text-color); }
      .bar { height: 2px; margin: 4px 8px 0; background: var(--divider-color); }
      .bar div { height: 100%; background: var(--primary-color); }
      `,
      `
      <div class="row">
        <button data-action="more-info" class="thumb" aria-label="More information">
          <img alt="" hidden>
          <ha-icon icon="mdi:speaker"></ha-icon>
        </button>
        <div class="text">
          <div class="title ellipsis"></div>
          <div class="artist ellipsis"></div>
        </div>
        <button data-action="play-pause" class="play-pause">
          <ha-icon icon="mdi:play"></ha-icon>
        </button>
        <button data-action="next" class="next" aria-label="Next">
          <ha-icon icon="mdi:skip-next"></ha-icon>
        </button>
      </div>
      <div class="bar" hidden><div></div></div>
      ${VOLUME_TEMPLATE}
      `
    );
    const image = this._$(".thumb img");
    const icon = this._$(".thumb ha-icon");
    image.addEventListener("load", () => {
      image.hidden = false;
      icon.hidden = true;
    });
    image.addEventListener("error", () => this._clearThumb());
  }

  _clearThumb() {
    this._$(".thumb img").hidden = true;
    this._$(".thumb ha-icon").hidden = false;
  }

  _update() {
    if (!this._hass || !this._config) return;
    const state = this._showState();
    if (!state) return;
    const usable = state.state !== "unavailable";

    const picture = state.attributes.entity_picture || null;
    if (picture !== this._picture) {
      this._picture = picture;
      if (picture) this._$(".thumb img").src = this._hass.hassUrl(picture);
      else this._clearThumb();
    }

    const { title, artist } = this._nowPlaying();
    this._$(".title").textContent = title;
    this._$(".artist").textContent = artist || this._name;
    this._updatePlayPause(usable);
    this._setControl(".next", this._supports(FEATURE.NEXT), usable);
    this._updateBar();
    this._updateVolume(usable);
  }

  _updateBar() {
    const state = this._state;
    if (!state) return;
    const duration = state.attributes.media_duration;
    const position = currentPosition(state);
    const bar = this._$(".bar");
    bar.hidden = !duration || position == null;
    if (!bar.hidden) {
      bar.firstElementChild.style.width = `${(position / duration) * 100}%`;
    }
  }

  _tick() {
    if (this._state?.state === "playing") this._updateBar();
  }
}

class LoeweStatusCard extends LoeweCard {
  getCardSize() {
    return 4;
  }

  getGridOptions() {
    return { columns: 6, min_columns: 4, rows: "auto" };
  }

  // The speaker's other entities, found by translation key so that renames
  // don't break the card.
  _related() {
    const related = {};
    const entities = this._hass?.entities || {};
    const deviceId = entities[this._config?.entity]?.device_id;
    if (!deviceId) return related;
    for (const entry of Object.values(entities)) {
      if (entry.device_id === deviceId && entry.platform === PLATFORM) {
        related[entry.translation_key] = entry.entity_id;
      }
    }
    return related;
  }

  _watched() {
    return [this._config?.entity, ...Object.values(this._related())];
  }

  _build() {
    if (this._$(".rows")) return;
    this._render(
      `
      .header {
        display: flex; align-items: center; gap: 8px; padding: 8px 8px 0 16px;
        color: var(--primary-text-color);
      }
      .header .name { flex: 1; min-width: 0; font-size: 16px; font-weight: 500; }
      .rows { padding: 4px 16px 12px; }
      .status-row {
        display: flex; justify-content: space-between; gap: 16px;
        width: 100%; height: auto; padding: 6px 0; border-radius: 0;
        font: inherit; text-align: left;
      }
      .status-row:hover:not(:disabled) { background: none; }
      .status-row:disabled { opacity: 1; }
      .status-row .label { flex: none; color: var(--secondary-text-color); }
      .status-row .value { min-width: 0; color: var(--primary-text-color); }
      .status-row.alert .value { color: var(--warning-color); font-weight: 500; }
      `,
      `
      <div class="header">
        <span class="name ellipsis"></span>
        <button data-action="more-info" aria-label="More information">
          <ha-icon icon="mdi:dots-vertical"></ha-icon>
        </button>
      </div>
      <div class="rows"></div>
      `
    );
  }

  _rowsData() {
    const state = this._state;
    const { attributes } = state;
    const yesNo = (value) => (value ? "Yes" : "No");
    const rows = [
      { label: "State", value: this._hass.formatEntityState(state) },
      { label: "Source", value: attributes.source || "—" },
    ];
    if (attributes.cast_connected != null) {
      rows.push({
        label: "Cast connected",
        value: yesNo(attributes.cast_connected),
      });
    }
    if (attributes.airplay_session != null) {
      rows.push({
        label: "AirPlay session",
        value: yesNo(attributes.airplay_session),
      });
    }
    const related = this._related();
    const addEntity = (key, label, alert) => {
      const entity = this._hass.states[related[key]];
      if (!entity) return;
      rows.push({
        label,
        value: this._hass.formatEntityState(entity),
        entity: entity.entity_id,
        alert: alert?.(entity),
      });
    };
    addEntity("playfi_firmware", "Play-Fi firmware");
    addEntity("cast_firmware", "Cast firmware");
    addEntity(
      "update_available",
      "Cast update",
      (entity) => entity.state === "on"
    );
    return rows;
  }

  _update() {
    if (!this._hass || !this._config) return;
    if (!this._showState()) return;
    this._$(".name").textContent = this._name;
    const rows = this._rowsData().map(({ label, value, entity, alert }) => {
      const row = document.createElement("button");
      row.className = "status-row";
      row.classList.toggle("alert", Boolean(alert));
      if (entity) {
        row.dataset.action = "more-info";
        row.dataset.entity = entity;
      } else {
        row.disabled = true;
      }
      const labelEl = document.createElement("span");
      labelEl.className = "label";
      labelEl.textContent = label;
      const valueEl = document.createElement("span");
      valueEl.className = "value ellipsis";
      valueEl.textContent = value;
      row.append(labelEl, valueEl);
      return row;
    });
    this._$(".rows").replaceChildren(...rows);
  }
}

const CARDS = [
  [
    "loewe-klang-player-card",
    LoewePlayerCard,
    "Loewe klang player",
    "Cover art, transport controls, progress and volume in one card.",
  ],
  [
    "loewe-klang-mini-card",
    LoeweMiniCard,
    "Loewe klang mini player",
    "Compact now playing with play/pause, next and volume.",
  ],
  [
    "loewe-klang-status-card",
    LoeweStatusCard,
    "Loewe klang status",
    "Current source, Cast and AirPlay connections, and firmware.",
  ],
];

window.customCards = window.customCards || [];
for (const [type, element, name, description] of CARDS) {
  if (customElements.get(type)) continue;
  customElements.define(type, element);
  window.customCards.push({ type, name, description, preview: true });
}
