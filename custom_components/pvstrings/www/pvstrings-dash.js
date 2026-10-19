/* ============================================================================
 * PV Strings Dashboard — cards + strategy for the PV Strings integration
 * https://github.com/doccodyblue/ha-pvstrings-dash
 *
 * One ES module, no build step: this file is both source and artifact.
 *
 * SECTION INDEX (in dependency order — search for "SECTION:"):
 *   HEADER    version, module constants
 *   CONST     translation-key taxonomy, feature table
 *   I18N      de/en dictionary, t()
 *   THEME     palette tokens, base CSS, hatch pattern
 *   FMT       number/date/energy formatting (Intl, HA timezone)
 *   DATA      websocket wrappers, registry model, statistics helpers
 *   UI        problem panel, withheld chip, tooltip, base card class
 *   CARD:SKYMAP / CARD:FORECAST / CARD:CONVERSION / CARD:NOWCAST
 *   CARD:SENSORCHECK / CARD:CALIBRATION / CARD:CURVE / CARD:CHAIN
 *   CARD:DAILY / CARD:HOURPROFILE / CARD:KVTABLE / CARD:MATURITY
 *   CARD:LEARNING / CARD:HEALTH / CARD:THERMAL
 *   STRATEGY  registry -> generated dashboard
 *   REGISTER  customElements.define + customCards/customStrategies
 *
 * Design rules (SPEC §0 — every render path obeys them):
 *   1. "Not yet" and "nothing" never look the same.
 *   2. Every derived number is traceable to its inputs in <= 1 click.
 *   3. A card that cannot draw something says why. Never an empty panel.
 * ========================================================================== */

/* ============================ SECTION: HEADER ============================ */

const PVS_VERSION = "0.22.1";
const PVS_MIN_INTEGRATION = "1.8.0";

/* ============================ SECTION: CONST ============================= */

// Entity taxonomy: the integration's translation_keys. Entity ids are
// language-dependent (German install -> German ids), so identification goes
// through the registry + these keys, never through entity_id patterns.
const PLANT_KEYS = [
  "forecast_today", "forecast_remaining", "forecast_tomorrow",
  "forecast_day_after", "forecast_next_hour", "potential_now",
  "peak_hour_today", "produced_today", "power_now", "deviation_yesterday",
  "wmape_7d", "wmape_30d", "bias_7d", "wmape_day_ahead_7d",
  "wmape_day_ahead_30d", "bias_day_ahead_30d", "savings_today",
  "savings_month", "savings_total", "amortisation",
  "rain_probability_tomorrow", "model_observations", "ghi_forecast",
  "strings_detail", "collector_health",
  // conversion layer (>= 1.20) — only published when an output path is
  // configured; absence is normal, not an error
  "forecast_ac_today", "forecast_ac_tomorrow",
];
const STRING_KEYS = [
  "string_sky_map", "string_shading_now", "string_forecast_today",
  "string_forecast_remaining", "string_forecast_tomorrow",
  "string_potential_now", "string_produced_today",
  // modelled cell temperature (>= 1.24): diagnostic, unknown at night
  "string_cell_temperature",
];
const GROUP_KEYS = ["group_forecast_remaining",
  // conversion layer (>= 1.20): direct groups publish _ac, storage groups
  // publish _battery_charge; a group without an output path has neither
  "group_forecast_ac", "group_forecast_battery_charge"];

// unique_id suffixes -> translation_key (registry rows may omit
// translation_key on older cores; the unique_id suffix is stable).
const UNIQUE_SUFFIX_TO_KEY = {
  sky_map: "string_sky_map",
  shading_now: "string_shading_now",
  // string_* and plant keys share suffixes (forecast_today etc.); the device
  // model ("PV plant" vs "PV string") disambiguates in buildModel().
};

// Feature detection (README "The contract"): the question is "can I draw
// this", never "what release is this". Tests run on an entity's attributes.
const FEATURES = {
  forecast_list: {
    test: (a) => Array.isArray(a?.forecast),
    attr: "forecast", since: "1.0.0",
  },
  chain_steps: {
    test: (a) => a?.forecast?.[0]?.physics_kwh !== undefined,
    attr: "forecast[].physics_kwh", since: "1.8.0",
  },
  unshaded: {
    test: (a) => a?.forecast?.[0]?.unshaded_kwh !== undefined,
    attr: "forecast[].unshaded_kwh", since: "1.7.0",
  },
  // the plant's irradiance sensor gained the nowcast block (>= 1.21); the
  // reason field is the interesting half when it is inactive
  nowcast: {
    test: (a) => a != null && "nowcast_active" in a,
    attr: "nowcast_active", since: "1.21.0",
  },
  // the irradiance sensor graded against an independent reanalysis archive
  // (>= 1.26). Tested on the block, not on its ratio: a plant whose sensor
  // has no referenced hour yet publishes the block full of nulls, and that
  // is a state to draw, not a missing feature.
  sensor_check: {
    test: (a) => a?.sensor_check != null && "by_elevation" in a.sensor_check,
    attr: "sensor_check", since: "1.26.0",
  },
  // conversion entities must say what they are — without output_path and
  // curve_source the card would silently assume AC semantics
  conv_output: {
    test: (a) => a != null && "output_path" in a && "curve_source" in a,
    attr: "output_path & curve_source", since: "1.20.0",
  },
  // the 30 d day-ahead sensor gained the per-hour fold of its own scored
  // pairs (>= 1.23). Deliberately tested on the key, not on a non-empty
  // list: the profile is published before the accuracy state is, so an
  // empty list would be a state to draw, not a missing feature.
  hourly_profile: {
    test: (a) => Array.isArray(a?.hourly_profile),
    attr: "hourly_profile", since: "1.23.0",
  },
  // the string chain rows gained the heat share (>= 1.24): thermal is NOT
  // a fourth factor — it is already inside physics_kwh and only says how
  // much of it is heat. Tested on any row, because night rows carry 1.0.
  thermal: {
    test: (a) => a?.forecast?.[0]?.thermal !== undefined,
    attr: "forecast[].thermal", since: "1.24.0",
  },
  // the collector sensor's counters — the health strip reads them all
  collector: {
    test: (a) => a != null && "intervals_written" in a && "write_errors" in a,
    attr: "intervals_written & write_errors", since: "1.8.0",
  },
  sky_cells: {
    test: (a) => Array.isArray(a?.cells),
    attr: "cells", since: "1.8.0",
  },
  sky_ratio: {
    test: (a) => !a?.cells?.length || a.cells[0].ratio !== undefined,
    attr: "cells[].ratio", since: "1.15.0",
  },
  // Presence check, deliberately separate from sky_ratio:
  // level: null is a valid state (single-string plants and strings without
  // enough shared epochs have none — structural, not "not yet");
  // a MISSING key is the contract error (problem panel).
  sky_level: {
    test: (a) => a != null && "level" in a && "fit_method" in a,
    attr: "level & fit_method", since: "1.18.0",
  },
};

function requireFeatures(stateObj, keys) {
  const a = stateObj?.attributes;
  const missing = [];
  for (const k of keys) {
    const f = FEATURES[k];
    if (!f.test(a)) missing.push({ attr: f.attr, since: f.since });
  }
  return missing.length ? { ok: false, missing } : { ok: true };
}

/* ============================ SECTION: I18N ============================== */

const STR = {
  en: {
    // shared
    "not_available": "not available",
    "entity_missing": "Entity not found: {entity}",
    "no_entity_config": "No entity configured. Pick a PV Strings sensor.",
    "needs_attr": "needs {attr} on {entity} (PV Strings {since} or newer)",
    "found_without": "Found an entity without it.",
    "stats_unavailable": "No recorder statistics for {entity} — check the recorder include/exclude filters.",
    "more_info": "show source entity",
    "kwh": "kWh",
    "today": "Today",
    "tomorrow": "Tomorrow",
    "remaining": "Remaining",
    "now": "now",
    "in_progress": "hour in progress ({min} min)",
    "not_yet_hour": "not yet — hour is in the future",
    "day_running": "today, still running",
    // sky map
    "sky_level": "level",
    "sky_fit_differential": "differential",
    "sky_fit_absolute": "absolute",
    "sky_level_tip": "Clear-view level relative to physics — 1.05 means: delivers 5 % above physics where nothing is in the way.",
    "sky_level_none": "no level — a single-string plant, or not enough shared epochs with the sibling strings",
    "sky_cells": "{n} cells observed",
    "sky_share_of_year": "≈ {pct}% of the year's sun path",
    "sky_no_cells": "No sky cells learned yet — the map fills in as the sun crosses new positions. {obs} raw observations collected so far.",
    "sky_unobserved": "never observed — not “no loss”",
    "sky_loss": "loss",
    "sky_loss_clear": "loss (clear day)",
    "sky_pooled": "pooled (all year)",
    "sky_season_ascending": "season: ascending sun (Dec–Jun)",
    "sky_season_descending": "season: descending sun (Jun–Dec)",
    "sky_ratio_gt1": "ratio above 1.0 — measurement above the physics envelope",
    "sky_sun_below": "sun below 3° — no shading value at night",
    "sky_sun_outside": "sun outside the observed window",
    "sky_layer_pooled": "Pooled",
    "sky_layer_ascending": "Dec–Jun",
    "sky_layer_descending": "Jun–Dec",
    "sky_obs": "observations",
    // forecast
    "fc_forecast": "Forecast",
    "fc_unshaded": "Unshaded",
    "fc_actual": "Actual",
    "fc_no_hours": "The forecast list is empty — the weather source has not covered any hours yet.",
    "fc_gap": "hour not covered by the weather source",
    "fc_known_shadow": "known shadow",
    "fc_delta": "actual vs forecast",
    "fc_group_unshaded": "Group sensors publish no separate unshaded series (the attribute is a copy) — unshaded hidden.",
    "fc_actual_unresolved": "Actual production hidden: could not resolve the production sensor for this entity.",
    "fc_hero_ist": "actual today",
    "fc_hero_prog": "forecast today",
    "fc_hourly_fallback": "actual drawn from hourly statistics (no 5-minute power data available)",
    // conversion (AC / battery charge)
    "conv_not_ready": "waiting for forecast data",
    "conv_dc": "DC potential",
    "conv_out_direct": "AC potential",
    "conv_out_storage": "battery charge",
    "conv_hero_dc": "DC today",
    "conv_hero_direct": "AC today",
    "conv_hero_storage": "charge today",
    "conv_eff_today": "conversion today",
    "conv_eff_tip_direct": "Day total AC / day total DC — after the conversion curve and the AC rating. Hardware potential: never capped at regulatory limits.",
    "conv_eff_tip_storage": "Day total charge / day total DC — after the charge path's conversion curve. DC energy into the storage, not AC.",
    // nowcast (the plant forecast reacting to its own irradiance sensor).
    // Deliberately NOT the same thing as truth_source "nowcast" below.
    "nc_title": "Nowcast",
    "nc_active": "running",
    "nc_inactive": "not running",
    "nc_kt": "measured clearness",
    "nc_fc": "forecast",
    "nc_vs_brighter": "brighter than forecast",
    "nc_vs_darker": "darker than forecast",
    "nc_vs_same": "as forecast",
    "nc_fc_tip": "Forecast clearness = the weather source's irradiance for now divided by the clear-sky value. Measured is the mean of the last quarter hour, so near sunrise and sunset the two can drift a few hundredths apart on their own.",
    "nc_weight": "weight on the next interval",
    "nc_halflife": "half-life",
    "nc_sky_calm": "calm sky",
    "nc_sky_broken": "broken sky",
    "nc_sky_unknown": "sky unclassified",
    "nc_spread": "error scatter",
    "nc_intervals": "{n} measured intervals",
    "nc_trust": "damping",
    "nc_trust_tip": "The correction is damped while the bias model behind it is still thin.",
    "nc_axis_min": "minutes ahead",
    "nc_decay_note": "The fade is computed from the two published figures (weight now, half-life) — the integration publishes no per-interval curve. Reach is capped at two hours.",
    "nc_reason_no_source": "no irradiance sensor configured — nothing to correct against",
    "nc_reason_no_measurement": "the sensor is delivering no readings",
    "nc_reason_too_dark": "too dark — at night there is nothing to correct",
    "nc_reason_thin_window": "not enough measured intervals yet (normal in the first minutes after a restart)",
    "nc_reason_stale": "the readings are too old to correct with",
    "nc_reason_frozen_sensor": "the sensor is not moving — its readings are not trusted",
    "nc_reason_learning_off": "learning is switched off",
    "nc_reason_other": "not running: {reason}",
    "nc_not_confused": "Not the same as “learned against the source's own short-horizon run” in the source-bias table: that phrase describes where the bias is learned from, not what corrects the forecast here.",
    // ---- irradiance sensor check (PV Strings >= 1.26) --------------------
    // The plant's own irradiance sensor held against an independent
    // reanalysis archive, folded by sun elevation. Diagnosis only, and the
    // card has to say so: nothing here corrects the forecast.
    "sc_title": "Irradiance sensor vs. archive",
    "sc_overall": "overall",
    "sc_axis_elev": "sun elevation",
    "sc_reference": "reference 1.0",
    "sc_measured": "measured / reference",
    "sc_band_unusable": "not yet",
    "sc_band_why": "A band speaks once it holds {kwh} kWh/m² of reference across {days} separate days.",
    "sc_evidence": "{hours} hours · {days} days · {kwh} kWh/m² reference",
    "sc_sources": "reference: {sources}",
    "sc_pending": "{n} hours still waiting for their reference — the archive runs about a week behind, which is normal.",
    "sc_never_referenced": "no hour carries a reference yet — run pvstrings.backfill_irradiance_check once, and only over a period in which the sensor stood in the same place, clean and level",
    "sc_no_evidence": "no elevation band carries enough evidence yet",
    "sc_slope": "spread",
    "sc_slope_tip": "Highest minus lowest usable band. Flat is a calibration error, rising towards noon is a spectral one — that single number is what separates the two.",
    "sc_tilt": "east − west",
    "sc_tilt_tip": "East minus west at matched sun elevations. From ±0.08 the sensor is probably not level; a north-south tilt hides from this test entirely.",
    "sc_note": "Diagnosis only — none of this corrects the forecast.",
    "sc_tip_ratio": "measured / reference",
    "sc_tip_hours": "hours",
    "sc_tip_days": "days",
    "sc_tip_ref": "reference",
    // The integration publishes its verdict in English, from a closed set of
    // phrases. Translated here rather than passed through — a German
    // dashboard stays German.
    "sc_dir_low": "reads low",
    "sc_dir_high": "reads high",
    "sc_dir_crosses": "crosses the reference",
    "sc_shape_drops": "{dir}, and more so as the sun drops",
    "sc_shape_rises": "{dir}, and more so as the sun rises — unusual",
    "sc_shape_flat": "{dir} by about the same amount at every sun height",
    "sc_read_thin": "not enough evidence yet",
    "sc_read_tilt": "differs east to west — check that the sensor is level",
    "sc_read_oneband": "one elevation band only — no shape yet",
    "sc_read_uneven": "uneven across the sky, with no clear trend",
    "sc_read_agrees": "agrees with the reference",
    "sc_read_close": "close to the reference, but not evenly so",
    // ---- calibration trial (PV Strings >= 1.26) ---------------------------
    // A second, complete model branch reading the same sensor through a
    // correction curve, learning its own model from scratch. Diagnosis of a
    // trial, never a setting — and for the first weeks it has nothing to draw
    // but its own progress, which must read as "collecting", not "broken".
    "cal_title": "Calibration trial",
    "cal_running": "running",
    "cal_not_needed": "no trial needed",
    "cal_no_evidence": "waiting for evidence",
    "cal_off": "switched off",
    "cal_offer": "That is what the check found. A trial would learn the whole model again through a correction curve and be published only if it proved better. It costs a second forecast and a second learn cycle every hour, so it stays off until you ask for it:",
    "cal_where": "Settings → Devices & services → PV Strings → Configure → Advanced → “Run the calibration trial”",
    "cal_off_nofinding": "The trial is switched off. The sensor check has nothing to say about this sensor yet, so there is nothing a correction curve could be built from.",
    "cal_loading": "reading the trial…",
    "cal_days": "{days} of {needed} clear days",
    "cal_hours_sub": "{archived} hours archived, {clear} of them on clear days",
    "cal_hours_none": "the first hour is archived on the hour — the trial started today",
    "cal_published_live": "The forecast still comes from the existing model.",
    "cal_published_cal": "The forecast now comes from the calibrated model.",
    "cal_curve": "correction curve",
    "cal_curve_range": "{from}× at {low}° → {to}× at {high}°",
    "cal_curve_capped": "curve capped",
    "cal_curve_rev": "revision {rev}",
    "cal_h_da": "evening before",
    "cal_h_now": "short-term",
    "cal_h_tip": "The evening-before issue is where the bias layer and the weather class decide; the nowcast does not reach that far. Short-term includes it.",
    "cal_leg_live": "existing model",
    "cal_leg_cal": "calibrated branch",
    "cal_leg_ref": "reference 1.0",
    "cal_axis_hour": "hour of day",
    "cal_chart_empty": "no scored hours yet",
    "cal_chart_empty_sub": "the trial is collecting clear days",
    "cal_col_live": "existing",
    "cal_col_cal": "calibrated",
    "cal_row_worst": "worst hour",
    "cal_row_worst_tip": "How far the worst hour of the day is from getting it right. One number that catches both failings: a branch that is uniformly wrong cannot improve it by flattening, and one whose average is right cannot hide a morning at 1.46 behind an afternoon at 0.70.",
    "cal_row_level": "level",
    "cal_row_level_tip": "Forecast over actual across every scored hour. It is watched alongside the shape, because a branch that is uniformly low would otherwise win on shape alone while being wrong all day.",
    "cal_not_yet": "not yet",
    "cal_improvement": "worst hour improved by",
    "cal_improvement_needed": "needs {pct}",
    "cal_waiting": "After {needed} clear days the criterion decides: the worst hour has to lose {improvement} of its error without the level drifting by more than {drift}.",
    "cal_decided": "The criterion is met — the calibrated branch is measurably better.",
    "cal_decided_note": "Switching it on stays a decision of yours; nothing switches itself.",
    "cal_blocking_head": "Not decided yet:",
    "cal_block_days": "only {n} clear days so far, {m} needed",
    "cal_block_noprofile": "one of the branches has no hourly profile yet",
    "cal_block_nolevel": "one of the branches has no level yet",
    "cal_block_worst": "worst hour {a} against {b} — needs {c}",
    "cal_block_drift": "level {a} drifted further than {b}",
    "cal_note": "Every learned layer was trained while reading a sensor that runs low. Rather than correcting that after the fact, a second branch learns the model again through a curve — and is published only if it proves better.",
    "cal_note_idle": "The check finds nothing about this sensor that a correction curve would fix, so no second branch is running.",
    "cal_note_wait": "No second branch yet: a correction curve only comes into being once the sensor check has gathered evidence enough to justify one.",
    "help_calibration": "**Calibration trial**: the irradiance sensor reads low by an amount that depends on the sun's height, and every layer this plant has learned — the shading map, the source bias, the weather corrections — was trained while reading it. Putting a factor in front of the sensor now would correct the same error twice, because those layers have absorbed parts of it already. So a **second, complete model branch** runs alongside: same plant, same weather, but the sensor read through a correction curve, and its own model learned from scratch. Both branches are scored on the same clear hours, and the card shows their forecast-over-actual by hour of day against 1.0. The criterion was written down **before** the trial began: after the number of clear days named above, the worst hour of the day has to lose half its error, and the overall level must not drift by more than 0.02 while it happens. Until then the published forecast is the existing one, and the empty chart means the trial is still collecting — not that anything is wrong.",
    "help_sensorcheck": "**Irradiance sensor check**: a cheap weather station does not measure irradiance. It measures illuminance with a diode weighted for the human eye and divides by a constant, and that constant holds for exactly one reference case — the lower the sun, the redder its light and the larger the share the diode never sees. Every closed hour is held against an independent reanalysis archive and folded by sun elevation; **1.0 means the sensor agrees with the reference**. A flat error is a calibration factor, one that grows towards the horizon is a spectral error, and a gap between east and west is a sensor that is not level. Bands that have not gathered a kilowatt-hour of reference across five days yet are drawn as a hatched stub, never as a value. **Nothing here corrects the forecast** — the nowcast, the source-bias layer and the sky map have absorbed parts of this error already, so a factor in front of them would correct it twice.",
    "conv_curve_datasheet": "datasheet curve",
    // `custom` is hand-entered support points — a claim by the owner, not a
    // measurement. Since measured curves now exist, the wording must not
    // blur the two.
    "conv_curve_custom": "self-entered curve",
    "conv_curve_learned": "learned curve",
    "conv_curve_prior": "on top of {prior}",
    // learned-curve card
    "curve_title": "Conversion curve",
    "curve_learned": "learned curve",
    "curve_applied": "applied curve",
    "curve_measured": "measured",
    "curve_maxload": "highest load seen",
    "curve_maxload_tip": "The highest load this plant has ever reached. Points beyond it can never gather evidence.",
    "curve_coverage_tip": "Share of the reachable support points that measurement has moved.",
    "curve_unreachable": "never reached",
    "curve_unreachable_band": "never reached",
    "curve_collecting": "Learning is on and measurements are coming in — points move gradually with their evidence, there is no threshold they jump at.",
    "curve_blocked_derived": "This inverter reports a calculated AC value (input × a fixed factor), not a measurement — so the curve stays on the datasheet. That is a property of the hardware, not a fault. With a real metering socket on the AC output, entered as the measured AC power, learning runs. The socket must feed the inverter alone: anything else on it distorts exactly the low-load end where the curve is most interesting.",
    "curve_blocked_other": "Learning is paused for this curve: {reason}. The applied curve stays on its prior.",
    "curve_tip_load": "load",
    "curve_point_moving": "measurement is in — the point follows it in proportion to its evidence",
    "curve_point_nodata": "no measurement at this load yet",
    "curve_point_blocked": "measurement discarded — see the note above",
    "curve_point_unreachable": "never reached (highest load {max} %) — stays on the datasheet and is not counted in maturity",
    "curve_prior_series": "prior (datasheet)",
    "curve_coverage": "support points moved",
    "curve_delta": "correction vs prior (pp)",
    "curve_axis_load": "load (share of AC rating)",
    "curve_axis_eta": "efficiency",
    "curve_pending": "No learned curve for this group yet. The collector is gathering: {usable} of {rows} measured pairs have cleared the censoring check. Support points only move once the evidence at a point suffices.",
    "curve_silent": "Measurement pairs are set up for this group, but nothing is being collected (0 rows). This is not a waiting state — check the configured output sensor.",
    "curve_not_collected": "No measurement pairs are being collected for this group — no output sensor configured, or learning is off.",
    "curve_unsupported": "This integration version does not learn conversion curves.",
    "curve_gathering": "Learning is on, no support point has moved yet — every point still holds its prior. Evidence per point below.",
    "curve_point_learned": "learned from measurement",
    "curve_bins_missing": "The learning block carries no support points.",
    "curve_note": "Not learned: intervals at the AC rating (the output stops following the input), below 1 % load (the inverter's own consumption dominates) and anything the censoring marked as curtailed. Learned points are capped at ±5 percentage points around the prior — one day with a broken sensor cannot rewrite the curve. A whole curve is discarded when the measurement is too flat to be one.",
    "conv_curve_neutral": "unconverted (output = DC)",
    "conv_curve_fixed_factors": "fixed factors",
    "conv_fixed_factors_tip": "Fixed per-stage factors applied to DC (e.g. MPPT × charge) — configured values, not measured.",
    "conv_neutral_note": "No conversion curve configured — the output equals DC by definition, this is not a measured 0 % loss.",
    "conv_clipped": "clipping {v} kWh",
    "conv_clipped_tip": "Energy above the inverter's AC rating — a hardware cap, not a conversion loss.",
    "conv_strip": "{out} / DC per hour",
    "conv_low_dc": "too little DC for a meaningful ratio",
    // chain
    "chain_title": "Forecast chain",
    "chain_physics": "raw physics",
    "chain_shading": "sky map",
    "chain_model": "per-string model",
    "chain_published": "published",
    "chain_measured": "measured",
    "chain_source_bias": "source bias ×{v} — applied upstream to the irradiance, already inside the physics figure. Shown as context, not a link.",
    "chain_discrepancy": "physics × shading × model ≠ published ({a} vs {b}). The factors published for this hour do not multiply out to the published value — please report it with the diagnostics download.",
    "chain_hour": "Hour",
    "chain_no_hour": "No forecast row for this hour.",
    "chain_needs_string": "This card reads the per-hour chain, which only string forecast sensors publish.",
    // daily
    "daily_title": "Day-ahead vs actual",
    "daily_soll": "Forecast (day-ahead)",
    "daily_ist": "Actual",
    "daily_no_issue": "no forecast issued that evening",
    "daily_provenance": "day-ahead = {entity} as recorded {date} {hour}:00 (recorder statistics)",
    "daily_provenance_own": "day-ahead = what was issued {date} {hour}:00, from the integration's own record",
    "daily_window_sum": "{n} days: forecast {soll} — actual {ist}",
    "daily_wabs": "weighted |error|",
    // accuracy / strategy
    "v_overview": "Overview",
    "v_strings": "Strings",
    "v_accuracy": "Accuracy",
    "v_nerd": "Nerd Dashboard",
    "s_today": "Today",
    "s_tomorrow": "Tomorrow & weather",
    "s_savings": "Savings",
    "s_groups": "Inverter groups",
    "s_conversion": "Conversion (AC / storage)",
    "nerd_conversion": "Conversion",
    "conv_path": "path",
    "conv_curve": "curve",
    "conv_stages": "stages",
    "conv_clipped_col": "clipping",
    "conv_today_col": "conversion today",
    "nerd_conv_evidence": "Learning — conversion evidence",
    "conv_ev_stage": "stage",
    "conv_stage_inverter": "inverter",
    "conv_stage_mppt": "MPPT",
    "conv_ev_loading": "loading collection evidence…",
    "conv_ev_admin": "Conversion learning evidence lives in the entry diagnostics, which need an admin user — hence empty here.",
    "conv_ev_unavailable": "The entry diagnostics could not be loaded — from here, whether collection runs cannot be checked.",
    "conv_ev_none": "The integration collects no conversion evidence yet — nothing is configured to learn from.",
    "conv_ev_silent": "collecting nothing",
    "conv_ev_note": "Usable = pairs cleared by the censoring check; it always trails the total by up to an hour, which is the gate working. A 0 / 0 row is configured but collecting nothing. A few hundred usable pairs per stage and sunny day is the realistic order of magnitude; there is no progress bar because no threshold defines \"enough\".",
    "s_daily": "Day by day",
    "nerd_explain_title": "What these numbers mean",
    "nerd_explain": "- **factor / n_eff** (learning buckets): the factor is the learned correction the physics forecast gets multiplied by, shown as a percentage — +5 % means \"reality delivered 5 % more than computed\" (factor 1.05, on hover). n_eff is the effective weight of evidence behind it (recent hours count more); small values mean the factor is still tentative.\n- **weather × daypart**: the plant learns separately per weather class and time of day. \"never seen\" means exactly that — this weather has not occurred at this time of day yet, which is itself a finding. The string × daypart layer has no such threshold: since integration 1.20.1 every bucket is published from its first observation and pulled towards 1.00 by how little evidence stands behind it — half strength at around ten observations, near-neutral below that. A factor close to 1.00 with a small n is thin, not switched off.\n- **Source bias (hour × horizon)**: the weather source's systematic error per local hour and forecast horizon, as a factor on irradiance. *measured* = learned against a real sensor; *nowcast* = only against the source's own short-horizon run — a much weaker claim.\n- **Sky map**: *level* is the string's clear-view level relative to physics — 1.05 means it delivers 5 % above physics where nothing is in the way. Where strings share enough epochs the map is fitted against the sibling strings (*differential*); a single string fits *absolutely* and has no level. On a differential map each cell's loss is the clear-day loss — what the shadow costs on a clear day; at runtime the integration scales it by the direct-light share, so an overcast day loses almost nothing. n is the beam-weighted observation count — smaller than before 1.18, which does not mean less data.\n- **Nowcast**: the forecast reacting to your own irradiance sensor. The measured clearness of the last quarter hour is blended into the coming intervals and fades back to the provider's forecast with a half-life that depends on how broken the sky is — reach is two hours, past hours are never touched. Inactive at night and without a sensor is the normal case, and then the reason is the interesting figure. Not to be confused with *nowcast* in the source-bias table, which says where the bias is learned from.\n- **Collection / censoring**: coverage is the share of 5-minute intervals actually captured — counted over daylight hours only (PV Strings ≥ 1.16), so a source that sleeps at night is not penalised. *lower bound* marks hours where the inverter was curtailed — real yield would have been higher, so the value only counts as a minimum.\n- **Skip reasons**: what the learn cycle deliberately did NOT learn from, and why. On a plant that learns nothing, this list is the entire diagnosis.\n- **Training maturity**: the weather bar is the evidence held across all weather × daypart buckets, relative to the most a bucket can ever hold (learning forgets slowly, so the count saturates — 100 % means \"as learned as it gets\", not \"finished\"; the tick marks where green begins — the point that in practice counts as fully learned). The shading bar is the share of the year's sun path each string has observed; it can only grow as fast as the calendar.\n- **Conversion (AC / battery charge)**: optional — appears once a group has an output path. AC is energy behind the inverter, capped at its AC rating when clipping applies but never at regulatory limits; battery charge is DC into the storage, whose discharge time is a control decision — the two are never added. The direct path runs on a load-dependent curve, from the datasheet or self-entered; where measured DC/AC pairs exist and the owner switches it on, the plant corrects that curve with its own measurement. The storage path runs on fixed factors. *Unconverted* means no curve is configured, not a measured 0 % loss.",
    "strategy_no_integration": "## PV Strings\nNo PV Strings entities found. Install and configure the [PV Strings integration](https://github.com/doccodyblue/ha-pvstrings) first — this dashboard builds itself from its sensors.",
    "missing_card": "**{key}** expected here, but no such entity exists on this device — it was not silently omitted. Check whether the integration version publishes it, or whether the entity is disabled.",
    // nerd
    "nerd_learning": "Learning — correction factors",
    "nerd_plant_buckets": "Plant: weather × daypart",
    "nerd_plant_hours": "Plant: weather × hour",
    "nerd_bucket_missing": "never seen",
    "cens_coverage": "coverage",
    "cens_curtailed": "curtailed",
    "nerd_truth_measured": "learned against a measured sensor",
    "nerd_truth_nowcast": "learned only against the source's own short-horizon run — a much weaker claim",
    "nerd_collection": "Collection",
    "nerd_censoring": "Censoring split (today)",
    "nerd_skips": "Learn-cycle skip reasons",
    "nerd_sky": "Sky maps",
    "maturity_title": "Training maturity",
    "maturity_weather": "Weather correction",
    "maturity_shading": "Shading (sky maps)",
    "maturity_buckets": "{seen} of {total} buckets seen",
    "maturity_no_lat": "location unknown — no sun-path reference",
    "kv_empty": "attribute {path} is empty or missing on {entity}",
    "factor": "factor",
    "weather_clear": "clear", "weather_partly_cloudy": "partly cloudy",
    "weather_overcast": "overcast", "weather_rain": "rain",
    "daypart_morning": "morning", "daypart_midday": "midday",
    "daypart_afternoon": "afternoon",
    "hourly_legend_less": "delivered less than computed",
    "hourly_legend_more": "delivered more",
    "hourly_note": "One column per hour, counted from solar noon; the time is when that hour starts today, and only today's daylight hours are shown. The hours began with the value of their old daypart and learn one by one from there, so within a daypart they look alike at first.",
    "hourly_note_rel": "One column per hour, in hours from solar noon. The hours began with the value of their old daypart and learn one by one from there, so within a daypart they look alike at first.",
    "vk_measured": "measured", "vk_lower_bound": "lower bound",
    "vk_reconstructed": "reconstructed",
    // savings provenance (PV Strings >= 1.22): both blocks are optional and
    // independent — a plant on a fixed tariff with a sane meter sees neither
    "nerd_price": "Savings — provenance",
    "price_win_today": "today", "price_win_total": "total",
    "price_recorded": "recorded", "price_configured": "fixed price",
    "price_hours": "hours priced",
    "price_import": "⌀ import", "price_export": "⌀ feed-in",
    "price_dropped": "export not credited",
    "price_none_yet": "no hour priced yet",
    "price_note": "Recorded and fixed price add up to the window's delivered energy. Fixed price is whatever no recorded price covered — hours from before the price sensor existed, gaps in it, and the side this tariff model values where only the other one was recorded. The hour count is hours carrying any recorded price, so the energy split is the authoritative one. The mean prices are energy-weighted, not arithmetic: 0.21 is a tariff, 21 is a hundredfold slip in the sensor's unit.",
    "price_dropped_note": "Export not credited is grid export the strings did not make in the same hour — a battery discharging, a second generator behind the meter, a reversed meter sign. It is not PV energy, and it is not a loss either.",
    "price_tou_caveat": "Annual estimate under a time-varying tariff: weighted by irradiance, not by price — winter has few hours, but expensive ones. Disappears once a full year is on the record.",
    // hourly profile of the day-ahead error (PV Strings >= 1.23)
    "hp_title": "Day-ahead error by hour",
    "hp_forecast": "announced", "hp_actual": "arrived",
    "hp_deviation": "deviation", "hp_days": "days",
    "hp_unit": "kWh/d",
    "hp_gap": "shortfall",
    "hp_sum_of": "{f} / {a} kWh over {n} scored days",
    "hp_days_scored_one": "1 day scored", "hp_days_scored_many": "{n} days scored",
    "hp_thin_one": "thin basis — 1 day", "hp_thin_many": "thin basis — {n} days",
    "hp_thin_hours_one": "1 hour on a thin basis", "hp_thin_hours_many": "{n} hours on a thin basis",
    "hp_too_high": "promised, not delivered", "hp_too_low": "delivered on top",
    // ---- nerd dashboard 0.13: status strip, factors as ±%, heatmap, help ----
    "s_status": "Status",
    // accuracy card (0.14): seven tiles and two paragraphs become one card
    "acc_title": "Accuracy",
    "acc_short": "Short-term",
    "acc_short_note": "continuously updated",
    "acc_dayahead": "Day-ahead",
    "acc_dayahead_note": "frozen the evening before",
    "acc_7d": "WMAPE 7 days", "acc_30d": "WMAPE 30 days",
    "acc_bias7": "bias 7 days", "acc_bias30": "bias 30 days",
    "acc_yesterday": "yesterday",
    "acc_window": "{d} of {n} days",
    "help_accuracy": "**Short-term** may correct itself during the day (and, since the nowcast, carries the sensor correction at horizon 0); **day-ahead** is frozen the evening before. The two are **not comparable until both windows are full** — the day counts under each figure say how far along it is.\n\n**WMAPE** = weighted mean absolute percentage error: the sum of all forecast errors divided by the sum of actual production — 10 % means the forecasts were off by 10 % in total, with sunny hours weighing more than dawn hours. **Bias** is the signed mean per day, in kWh: positive, the forecast ran high.",
    "help_daily": "**Day by day**: what was announced the evening before against what arrived. Blue is the day-ahead figure as issued at the issue hour, orange the metered yield; a hatched stub means no forecast was issued that evening, a hatched top means the day is still running. The chips switch between the plant and its strings — the same chart, one at a time.",
    "health_title": "Collection & learn cycle",
    "health_coverage": "coverage",
    "health_write_errors": "write errors",
    "health_weather": "weather source",
    "health_ok": "ok",
    "health_learn": "learn cycle",
    "health_learn_v": "{used} of {hours} hours learned",
    "health_skipped": "skipped",
    "health_censored": "censored hours",
    "health_station": "station air",
    "health_station_v": "temperature {t} % · wind {w} %",
    "health_station_none": "no station sensor, or before sunrise",
    "health_station_tip": "Share of today's daylight intervals for which the configured station sensor delivered a value — the check that measured air actually reaches the physics (PV Strings ≥ 1.24). Forward forecasts still run on the weather source's air; station readings only enter the reconstruction of past hours.",
    "health_all_good_one": "All {n} strings fully measured today: {iv} intervals, {cov} % coverage, nothing curtailed.",
    "health_all_good_many": "All {n} strings fully measured today: {iv} intervals, {cov} % coverage, nothing curtailed.",
    "health_no_cycle": "no learn cycle recorded yet",
    "health_learn_night": "night hours only — nothing to learn",
    "health_learn_night_tip": "The last cycle only had night hours to look at. That is the normal case outside daylight, not a fault.",
    "health_per_string": "coverage per string",
    "health_counters": "collector counters",
    "lbl_intervals_written": "intervals written",
    "lbl_events_seen": "events seen",
    "lbl_write_errors": "write errors",
    "lbl_watchdog_ticks": "watchdog ticks",
    "lbl_last_flush_duration_ms": "last flush (ms)",
    "lbl_hours_materialised": "hours materialised",
    "lbl_observations_used": "observations used",
    "lbl_observations_skipped": "observations skipped",
    "lbl_censored_hours": "censored hours",
    "lbl_bias_observations": "bias observations",
    "lbl_shading_observations": "shading observations",
    "lbl_ghi_hours_rejected": "irradiance hours rejected",
    "lbl_reconstructed_intervals": "reconstructed intervals",
    "lbl_skipped_because": "skipped because",
    "nerd_strings_table": "Strings: offset and daypart",
    "nerd_strings_hours": "Strings: offset and hour",
    "col_offset": "offset",
    "factor_tip_factor": "factor",
    "factor_tip_n": "evidence n_eff",
    "factor_thin": "thin evidence — pulled towards 0 %",
    "factor_legend": "correction the physics forecast is multiplied by; +7 % means reality delivered 7 % more than computed",
    "nerd_weather_source": "Weather source",
    "nerd_sensor": "Irradiance sensor",
    "bias_title": "Source bias by hour × horizon",
    "bias_legend_high": "source too high",
    "bias_legend_low": "source too low",
    "bias_thin": "thin",
    "bias_horizon": "horizon",
    "sky_cells_col": "cells",
    "sky_worst_col": "strongest shadow",
    "conv_stage_charge": "charging",
    "nerd_conv_paths": "Conversion — paths",
    "conv_ev_pairs_col": "usable / total",
    // thermal (PV Strings >= 1.24)
    "th_title": "Cell temperature",
    "th_air": "air",
    "th_wind": "wind",
    "th_cells": "cells",
    "th_effect": "effect now",
    "th_today": "today",
    "th_plant_today": "plant today",
    "th_by_heat": "by heat",
    "th_ref": "25 °C reference",
    "th_now": "now",
    "th_no_rows": "no hour with sun today yet — cell temperatures exist only while the modules see light",
    "th_note": "Modelled, not measured: the Sandia cell-temperature model for each mount type, fed with the forecast's air temperature, wind and plane irradiance. Below 25 °C cells the modules gain, above they lose — the share is already inside the physics figure, never a fourth factor.",
    "th_gain": "gain from cold",
    "th_loss": "loss to heat",
    "chain_thermal": "thermal ×{v} — {cell} °C cells at {air} °C air, {wind} m/s wind. Already inside the physics figure — shown as context, not a link.",
    // per-card help (replaces the explainer footer)
    "help_factors": "**Correction factors**: the physics forecast is multiplied by them — +7 % means reality delivered 7 % more than computed. The plant learns separately per weather class and time of day; *never seen* means this weather has not occurred at that time yet, which is itself a finding. Per string, an offset and a daypart layer sit on top; thin buckets are pulled towards 0 % by how little evidence stands behind them (half strength at about ten observations). Hover a cell for the raw factor and its evidence.",
    "help_bias": "**Source bias**: the weather source's systematic error per local hour and forecast horizon, as a correction on irradiance. Blue: the source announced more than arrived, the forecast is scaled down; orange: it announced too little. Faded cells rest on thin evidence. *Measured* means learned against a real sensor; *nowcast* only against the source's own short-horizon run — a much weaker claim.",
    "help_sky": "**Sky maps**: *level* is the string's clear-view yield relative to physics — 1.05 delivers 5 % above physics where nothing is in the way. Where strings share enough epochs the map is fitted against the sibling strings (*differential*); a single string fits *absolutely* and has no level. The strongest shadow names the sky sector (azimuth · elevation) with the largest clear-day loss.",
    "help_health": "**Collection**: coverage is the share of 5-minute intervals actually captured, counted over daylight hours only. **Learn cycle**: what the last cycle used and what it deliberately skipped, with the reason — on a plant learning nothing, that list is the whole diagnosis. Night hours do not count as skipped: there is nothing to learn from them. **Censored** hours are lower bounds (the inverter was curtailed, real yield would have been higher). Tables open only where a figure deviates.",
    "help_maturity": "**Training maturity**: the weather bar is the evidence held across all weather × daypart buckets, relative to the most a bucket can hold — learning forgets slowly, so 100 % means \"as learned as it gets\", not \"finished\"; the tick marks where green begins. The shading bar is the share of the year's sun path each string has observed; it grows no faster than the calendar.",
    "help_thermal": "**Cell temperature**: modelled per string from the forecast's air temperature, wind and plane irradiance — not measured. Cells above 25 °C lose output, below they gain; the effect is already inside the physics figure of the forecast chain, this card only makes it visible. A flat, insulated mount runs hotter than an open rack at the same air.",
    "help_conversion": "**Conversion**: AC is energy behind the inverter, capped at its AC rating when clipping applies but never at regulatory limits; battery charge is DC into the storage — the two are never added. Curves are configured (datasheet or self-entered); where measured DC/AC pairs exist and learning is on, the plant corrects that curve with its own measurement. *Unconverted* means no curve configured, not a measured 0 % loss.",
    "help_hp": "**Day-ahead error by hour**: the same scored pairs the 30-day figure is built from, folded by local hour, in kWh per scored day. The strip is the gap between promised and arrived — above the line the forecast promised more than came. Read it to see *where in the day* the forecast is weak, and what that costs; the percentage per hour is in the tooltip. Dimmed hours rest on fewer than three days.",
    "help_nowcast": "**Nowcast**: the forecast reacting to your own irradiance sensor. The measured clearness of the last quarter hour is blended into the coming intervals and fades back to the provider's forecast with a half-life that depends on how broken the sky is — reach is two hours, past hours are never touched. The blue mark is what the weather source expects for now on the same scale — the gap between the two is what the nowcast corrects. Inactive at night and without a sensor is the normal case; then the reason is the interesting figure.",
    "hp_no_hours": "No scored hour yet. The profile fills from the first complete day — it is not held back until the accuracy figures are.",
    // history (response services get_day / get_weeks)
    "hist_prev": "earlier", "hist_next": "later", "hist_pick": "pick a day",
    "hist_failed": "Could not load the history: {error}",
    "hist_needs_service": "Needs the {service} service — a newer PV Strings integration.",
    "hist_no_data": "Nothing was recorded for this day.",
    "hist_no_intervals": "Five-minute values reach back to {date} only — hourly values shown.",
    "hist_day_ahead": "evening before",
    "hist_day_ahead_gone": "The evening-before forecast is only kept for about five weeks; this day shows the last forecast before each hour.",
    "hist_hero_actual": "actual that day", "hist_hero_forecast": "forecast that day",
    "hist_issued_earlier": "from the run of {when} — the evening run was missing",
    "week_short": "wk {n}", "week_running": "week still running",
    "week_backfilled": "rebuilt",
    "week_backfilled_tip": "Computed after the fact from the forecasts that were published then; no model snapshot from that week exists.",
    "hp_sel_live": "30 days",
    "hp_note_week": "The same day-ahead pairs, for this week only, folded by local hour. Both halves are kWh per scored day, as on the month; above the line the hour promised more than arrived.",
    // learning progress
    "learn_title": "Learning progress",
    "learn_no_weeks": "no week recorded yet",
    "learn_no_baseline": "no week with a comparison against the forecast without learning yet",
    "learn_needs_hourly": "needs the hourly error on the weeks (PV Strings 1.25.2 or newer). Weeks recorded before that carry a daily figure, in which a morning and an afternoon error cancel — not a figure this comparison can use.",
    "learn_hero_less_one": "less forecast error than without learning · 1 week compared",
    "learn_hero_less_many": "less forecast error than without learning · {n} weeks compared",
    "learn_hero_more_one": "more forecast error than without learning · 1 week compared",
    "learn_hero_more_many": "more forecast error than without learning · {n} weeks compared",
    "learn_hero_equal_one": "no difference to the forecast without learning · 1 week compared",
    "learn_hero_equal_many": "no difference to the forecast without learning · {n} weeks compared",
    "learn_hero_wmape": "day-ahead error in wk {week} · {base} % without learning",
    "learn_hero_wmape_only": "day-ahead error in wk {week}",
    "learn_hero_thin": "not enough yet for a verdict — {have} of {need} complete weeks compared against learning switched off",
    "learn_leg_gain": "learning helped",
    "learn_leg_loss": "better without",
    "learn_leg_excluded": "not counted",
    "learn_note": "Per week: the day-ahead forecast error of this integration against the same integration with learning switched off — same weather run, same geometry, same hours, summed per hour. Above the line learning helped that week, below it the week would have been better without. Hatched weeks have a figure that cannot be added up, and the total names how many weeks it rests on.",
    "learn_tip_with": "error with learning", "learn_tip_without": "error without learning",
    "learn_tip_gain": "error avoided", "learn_tip_days": "days compared",
    "learn_tip_no_baseline": "no comparison for this week",
    "learn_tip_running": "still running — counts once the week is closed",
    "learn_tip_replayed": "rebuilt with today's code: not only learning on and off, two versions of the code as well. Not counted.",
    "learn_tip_mixed": "part of this week was compared live, part rebuilt with today's code. Not counted.",
    "learn_tip_unknown": "recorded before the integration noted where a comparison came from. Not counted.",
    "help_learning": "**One bar per week: what learning cost or saved.** The day-ahead forecast error of this integration, against the same integration with learning switched off — same weather run, same geometry, same hours. Not against bare physics: the learned shading map and the source correction count as learning too. The error is summed **per hour**, because a day's morning and afternoon errors cancel in a daily total and a forecast without a multiplicative correction cancels that way more readily — which would hand the comparison to the baseline for free. A week counts only if it is closed and its comparison was logged as it happened. Weeks rebuilt afterwards, part-rebuilt weeks, weeks recorded before the integration noted their origin, and the running week keep their bar hatched and stay out of the total: they also compare two versions of the code, so every later fix would count against the learning. The headline waits for three counted weeks — one week is a weather sample, not a verdict — and always names how many weeks it rests on.",
    "hp_note": "Where in the day the day-ahead forecast leaves energy on the table, from the same pairs the 30-day score is built from. Both halves are kWh per scored day — each hour divided by its own day count, so a week and the month stand side by side and the sum behind it is in the tooltip. The strip is the gap: above the line the hour promised more than arrived, below it reality did better. An hour carrying almost nothing shows almost no gap, which is the honest picture — its percentage is in the tooltip, where a question about the model can find it.",
    /* i18n-en-end */
  },
  de: {
    "not_available": "nicht verfügbar",
    "entity_missing": "Entity nicht gefunden: {entity}",
    "no_entity_config": "Keine Entity konfiguriert. Wähle einen PV-Strings-Sensor.",
    "needs_attr": "braucht {attr} auf {entity} (PV Strings {since} oder neuer)",
    "found_without": "Gefundene Entity hat es nicht.",
    "stats_unavailable": "Keine Recorder-Statistik für {entity} — Recorder-Filter (include/exclude) prüfen.",
    "more_info": "Quell-Entity anzeigen",
    "kwh": "kWh",
    "today": "Heute",
    "tomorrow": "Morgen",
    "remaining": "Rest",
    "now": "jetzt",
    "in_progress": "Stunde läuft ({min} min)",
    "not_yet_hour": "noch nicht — Stunde liegt in der Zukunft",
    "day_running": "heute, läuft noch",
    "sky_level": "Niveau",
    "sky_fit_differential": "differenziell",
    "sky_fit_absolute": "absolut",
    "sky_level_tip": "Freisicht-Niveau relativ zur Physik — 1,05 heißt: liefert 5 % über Physik, wo nichts im Weg ist.",
    "sky_level_none": "kein Niveau — Ein-Strang-Anlage oder zu wenige gemeinsame Epochen mit den Geschwister-Strängen",
    "sky_cells": "{n} Zellen beobachtet",
    "sky_share_of_year": "≈ {pct}% des Jahres-Sonnenwegs",
    "sky_no_cells": "Noch keine Himmelszellen gelernt — die Karte füllt sich, während die Sonne neue Positionen überstreicht. Bisher {obs} Roh-Beobachtungen.",
    "sky_unobserved": "nie beobachtet — nicht „kein Verlust“",
    "sky_loss": "Verlust",
    "sky_loss_clear": "Verlust (klarer Tag)",
    "sky_pooled": "gepoolt (ganzjährig)",
    "sky_season_ascending": "Saison: steigende Sonne (Dez–Jun)",
    "sky_season_descending": "Saison: fallende Sonne (Jun–Dez)",
    "sky_ratio_gt1": "Ratio über 1.0 — Messung über der Physik-Hüllkurve",
    "sky_sun_below": "Sonne unter 3° — nachts kein Verschattungswert",
    "sky_sun_outside": "Sonne außerhalb des beobachteten Fensters",
    "sky_layer_pooled": "Gepoolt",
    "sky_layer_ascending": "Dez–Jun",
    "sky_layer_descending": "Jun–Dez",
    "sky_obs": "Beobachtungen",
    "fc_forecast": "Prognose",
    "fc_unshaded": "Unverschattet",
    "fc_actual": "Ist",
    "fc_no_hours": "Die Prognoseliste ist leer — die Wetterquelle hat noch keine Stunden abgedeckt.",
    "fc_gap": "Stunde von der Wetterquelle nicht abgedeckt",
    "fc_known_shadow": "bekannter Schatten",
    "fc_delta": "Ist vs. Prognose",
    "fc_group_unshaded": "Gruppen-Sensoren publizieren keine eigene Unverschattet-Serie (das Attribut ist eine Kopie) — Unverschattet ausgeblendet.",
    "fc_actual_unresolved": "Ist-Produktion ausgeblendet: Produktionssensor für diese Entity nicht auflösbar.",
    "fc_hero_ist": "Ist heute",
    "fc_hero_prog": "Prognose heute",
    "fc_hourly_fallback": "Ist aus Stundenstatistik gezeichnet (keine 5-Minuten-Leistungsdaten verfügbar)",
    "conv_not_ready": "warte auf Prognosedaten",
    "conv_dc": "DC-Potenzial",
    "conv_out_direct": "AC-Potenzial",
    "conv_out_storage": "Akkuladung",
    "conv_hero_dc": "DC heute",
    "conv_hero_direct": "AC heute",
    "conv_hero_storage": "Ladung heute",
    "conv_eff_today": "Wandlung heute",
    "conv_eff_tip_direct": "Tagessumme AC / Tagessumme DC — nach Kennlinie und AC-Nennwert. Hardware-Potenzial: nie an Regel- oder Rechtslimits gedeckelt.",
    "conv_eff_tip_storage": "Tagessumme Ladung / Tagessumme DC — nach Kennlinie des Ladepfads. DC-Energie in den Speicher, kein AC.",
    "nc_title": "Nowcast",
    "nc_active": "läuft",
    "nc_inactive": "läuft nicht",
    "nc_kt": "gemessene Klarheit",
    "nc_fc": "Prognose",
    "nc_vs_brighter": "heller als Prognose",
    "nc_vs_darker": "dunkler als Prognose",
    "nc_vs_same": "wie Prognose",
    "nc_fc_tip": "Prognose-Klarheit = Einstrahlung der Wetterquelle für jetzt geteilt durch den Klarhimmel-Wert. Gemessen ist das Mittel der letzten Viertelstunde — um Sonnenauf- und -untergang laufen die beiden deshalb auch von selbst ein paar Hundertstel auseinander.",
    "nc_weight": "Gewicht im nächsten Intervall",
    "nc_halflife": "Halbwertszeit",
    "nc_sky_calm": "ruhiger Himmel",
    "nc_sky_broken": "aufgerissener Himmel",
    "nc_sky_unknown": "Himmel nicht klassifiziert",
    "nc_spread": "Fehlerstreuung",
    "nc_intervals": "{n} Messintervalle",
    "nc_trust": "Dämpfung",
    "nc_trust_tip": "Die Korrektur wird gedämpft, solange das Bias-Modell dahinter noch dünn ist.",
    "nc_axis_min": "Minuten voraus",
    "nc_decay_note": "Der Abfall ist aus den zwei veröffentlichten Zahlen gerechnet (Gewicht jetzt, Halbwertszeit) — die Integration liefert keine Kurve je Intervall. Reichweite maximal zwei Stunden.",
    "nc_reason_no_source": "kein Einstrahlungs-Sensor konfiguriert — es gibt nichts, wogegen korrigiert werden könnte",
    "nc_reason_no_measurement": "der Sensor liefert keine Messwerte",
    "nc_reason_too_dark": "zu dunkel — nachts gibt es nichts zu korrigieren",
    "nc_reason_thin_window": "noch zu wenige Messintervalle (normal in den ersten Minuten nach einem Neustart)",
    "nc_reason_stale": "die Messwerte sind zu alt, um damit zu korrigieren",
    "nc_reason_frozen_sensor": "der Sensor bewegt sich nicht — seinen Werten wird nicht vertraut",
    "nc_reason_learning_off": "das Lernen ist ausgeschaltet",
    "nc_reason_other": "läuft nicht: {reason}",
    "nc_not_confused": "Nicht dasselbe wie „nur gegen den Kurzfrist-Lauf der Quelle selbst gelernt“ in der Source-Bias-Tabelle: das beschreibt, woher der Bias gelernt wird, nicht was hier die Prognose korrigiert.",
    // ---- Sensor-Check (PV Strings >= 1.26) -------------------------------
    "sc_title": "Einstrahlungssensor gegen Archiv",
    "sc_overall": "gesamt",
    "sc_axis_elev": "Sonnenhöhe",
    "sc_reference": "Referenz 1,0",
    "sc_measured": "gemessen / Referenz",
    "sc_band_unusable": "noch nicht",
    "sc_band_why": "Ein Band sagt etwas aus, sobald es {kwh} kWh/m² Referenz an {days} verschiedenen Tagen trägt.",
    "sc_evidence": "{hours} Stunden · {days} Tage · {kwh} kWh/m² Referenz",
    "sc_sources": "Referenz: {sources}",
    "sc_pending": "{n} Stunden warten noch auf ihre Referenz — das Archiv hinkt rund eine Woche hinterher, das ist normal.",
    "sc_never_referenced": "noch keine Stunde hat eine Referenz — einmal pvstrings.backfill_irradiance_check ausführen, und nur über einen Zeitraum, in dem der Sensor am selben Ort stand, sauber und waagerecht",
    "sc_no_evidence": "noch kein Höhenband trägt genug Evidenz",
    "sc_slope": "Spanne",
    "sc_slope_tip": "Höchstes minus niedrigstes brauchbares Band. Flach ist ein Kalibrierfehler, zum Mittag hin steigend ein Spektralfehler — diese eine Zahl trennt die beiden.",
    "sc_tilt": "Ost − West",
    "sc_tilt_tip": "Ost minus West bei abgeglichenen Sonnenhöhen. Ab ±0,08 steht der Sensor vermutlich nicht waagerecht; eine Nord-Süd-Schräglage entgeht diesem Test ganz.",
    "sc_note": "Reine Diagnose — nichts davon korrigiert die Prognose.",
    "sc_tip_ratio": "gemessen / Referenz",
    "sc_tip_hours": "Stunden",
    "sc_tip_days": "Tage",
    "sc_tip_ref": "Referenz",
    "sc_dir_low": "liest zu niedrig",
    "sc_dir_high": "liest zu hoch",
    "sc_dir_crosses": "kreuzt die Referenz",
    "sc_shape_drops": "{dir}, und umso stärker, je tiefer die Sonne steht",
    "sc_shape_rises": "{dir}, und umso stärker, je höher die Sonne steht — ungewöhnlich",
    "sc_shape_flat": "{dir}, bei jeder Sonnenhöhe etwa gleich stark",
    "sc_read_thin": "noch nicht genug Evidenz",
    "sc_read_tilt": "unterscheidet sich von Ost nach West — prüfen, ob der Sensor waagerecht steht",
    "sc_read_oneband": "nur ein Höhenband — noch keine Form erkennbar",
    "sc_read_uneven": "ungleichmäßig über den Himmel, ohne klaren Trend",
    "sc_read_agrees": "stimmt mit der Referenz überein",
    "sc_read_close": "nah an der Referenz, aber nicht gleichmäßig",
    // ---- Kalibrierungsversuch (PV Strings >= 1.26) ------------------------
    "cal_title": "Kalibrierungsversuch",
    "cal_running": "läuft",
    "cal_not_needed": "kein Versuch nötig",
    "cal_no_evidence": "wartet auf Evidenz",
    "cal_off": "ausgeschaltet",
    "cal_offer": "Das hat die Prüfung gefunden. Ein Versuch würde das ganze Modell noch einmal durch eine Korrekturkurve lernen und nur veröffentlicht, wenn er sich als besser erweist. Er kostet eine zweite Prognose und einen zweiten Lernzyklus pro Stunde und bleibt deshalb aus, bis du ihn einschaltest:",
    "cal_where": "Einstellungen → Geräte & Dienste → PV Strings → Konfigurieren → Erweitert → „Kalibrierungsversuch laufen lassen“",
    "cal_off_nofinding": "Der Versuch ist ausgeschaltet. Der Sensor-Check hat zu diesem Sensor noch nichts zu sagen — es gibt also auch nichts, woraus sich eine Korrekturkurve bauen ließe.",
    "cal_loading": "Versuch wird gelesen…",
    "cal_days": "{days} von {needed} klaren Tagen",
    "cal_hours_sub": "{archived} Stunden archiviert, davon {clear} an klaren Tagen",
    "cal_hours_none": "die erste Stunde wird zur vollen Stunde archiviert — der Versuch läuft seit heute",
    "cal_published_live": "Die Prognose kommt weiterhin vom bisherigen Modell.",
    "cal_published_cal": "Die Prognose kommt jetzt vom kalibrierten Modell.",
    "cal_curve": "Korrekturkurve",
    "cal_curve_range": "{from}× bei {low}° → {to}× bei {high}°",
    "cal_curve_capped": "Kurve gedeckelt",
    "cal_curve_rev": "Fassung {rev}",
    "cal_h_da": "Vorabend",
    "cal_h_now": "Kurzfristig",
    "cal_h_tip": "Am Vorabend entscheiden Bias-Schicht und Wetterklassifikation; der Nowcast reicht nicht so weit. Kurzfristig zählt er mit.",
    "cal_leg_live": "bisheriges Modell",
    "cal_leg_cal": "kalibrierter Zweig",
    "cal_leg_ref": "Referenz 1,0",
    "cal_axis_hour": "Stunde des Tages",
    "cal_chart_empty": "noch keine gewerteten Stunden",
    "cal_chart_empty_sub": "der Versuch sammelt klare Tage",
    "cal_col_live": "bisher",
    "cal_col_cal": "kalibriert",
    "cal_row_worst": "schlechteste Stunde",
    "cal_row_worst_tip": "Wie weit die schlechteste Stunde des Tages davon entfernt ist, richtig zu liegen. Eine Zahl, die beide Fehler fängt: Ein durchgehend falscher Zweig kann sie nicht durch Glätten verbessern, und ein im Mittel richtiger kann keinen Vormittag bei 1,46 hinter einem Nachmittag bei 0,70 verstecken.",
    "cal_row_level": "Pegel",
    "cal_row_level_tip": "Prognose durch Ist über alle gewerteten Stunden. Er wird neben der Form mitgeprüft, weil ein durchgehend zu niedriger Zweig sonst allein über die Form gewinnen würde, während er den ganzen Tag falsch liegt.",
    "cal_not_yet": "noch nicht",
    "cal_improvement": "schlechteste Stunde verbessert um",
    "cal_improvement_needed": "gebraucht werden {pct}",
    "cal_waiting": "Nach {needed} klaren Tagen entscheidet das Kriterium: Die schlechteste Stunde muss {improvement} ihres Fehlers verlieren, ohne dass der Pegel um mehr als {drift} abdriftet.",
    "cal_decided": "Das Kriterium ist erfüllt — der kalibrierte Zweig ist nachweislich besser.",
    "cal_decided_note": "Das Umschalten bleibt deine Entscheidung; von allein schaltet nichts um.",
    "cal_blocking_head": "Noch nicht entschieden:",
    "cal_block_days": "erst {n} klare Tage, gebraucht werden {m}",
    "cal_block_noprofile": "einer der beiden Zweige hat noch kein Stundenprofil",
    "cal_block_nolevel": "einer der beiden Zweige hat noch keinen Pegel",
    "cal_block_worst": "schlechteste Stunde {a} gegen {b} — gebraucht wird {c}",
    "cal_block_drift": "Pegel {a} ist weiter abgedriftet als {b}",
    "cal_note": "Jede gelernte Schicht wurde beim Lesen eines zu niedrig messenden Sensors trainiert. Statt das nachträglich zu korrigieren, lernt ein zweiter Zweig das Modell durch eine Kurve neu — veröffentlicht wird er nur, wenn er sich als besser erweist.",
    "cal_note_idle": "Die Prüfung findet an diesem Sensor nichts, was eine Korrekturkurve beheben würde — deshalb läuft kein zweiter Zweig.",
    "cal_note_wait": "Noch kein zweiter Zweig: Eine Korrekturkurve entsteht erst, wenn der Sensor-Check genug Evidenz für eine gesammelt hat.",
    "help_calibration": "**Kalibrierungsversuch**: Der Einstrahlungssensor liest je nach Sonnenstand zu niedrig, und jede Schicht, die diese Anlage gelernt hat — Himmelskarte, Source-Bias, Wetterkorrekturen — wurde beim Lesen dieses Sensors trainiert. Einen Faktor davorzuschalten würde denselben Fehler doppelt korrigieren, weil jene Schichten Teile davon längst aufgenommen haben. Deshalb läuft ein **zweiter, vollständiger Modellzweig** daneben: dieselbe Anlage, dasselbe Wetter, aber der Sensor durch eine Korrekturkurve gelesen und das eigene Modell von Grund auf neu gelernt. Beide Zweige werden über dieselben klaren Stunden gewertet; die Karte zeigt Prognose durch Ist je Stunde des Tages gegen 1,0. Das Kriterium wurde **vor** dem Versuch festgeschrieben: Nach der oben genannten Zahl klarer Tage muss die schlechteste Stunde des Tages die Hälfte ihres Fehlers verlieren, ohne dass der Pegel dabei um mehr als 0,02 abdriftet. Bis dahin bleibt die veröffentlichte Prognose die bisherige, und das leere Diagramm heißt, dass der Versuch noch sammelt — nicht, dass etwas nicht stimmt.",
    "help_sensorcheck": "**Sensor-Check**: Eine billige Wetterstation misst keine Einstrahlung. Sie misst Beleuchtungsstärke mit einer aufs menschliche Auge gewichteten Diode und teilt durch eine Konstante — und die stimmt für genau einen Referenzfall: Je tiefer die Sonne steht, desto röter ihr Licht und desto größer der Anteil, den die Diode nicht sieht. Jede abgeschlossene Stunde wird gegen ein unabhängiges Reanalyse-Archiv gehalten und nach Sonnenstand gebändert; **1,0 heißt, der Sensor stimmt mit der Referenz überein**. Ein flacher Fehler ist ein Kalibrierfaktor, ein zum Horizont hin wachsender ein Spektralfehler, und ein Unterschied zwischen Ost und West ein Sensor, der nicht waagerecht steht. Bänder, die noch keine Kilowattstunde Referenz an fünf Tagen gesammelt haben, stehen als schraffierter Stummel da, nie als Wert. **Nichts davon korrigiert die Prognose** — Nowcast, Source-Bias-Schicht und Himmelskarte haben Teile dieses Fehlers bereits aufgenommen, ein Faktor davor würde doppelt korrigieren.",
    "conv_curve_datasheet": "Datenblatt-Kennlinie",
    "conv_curve_custom": "selbst eingetragen",
    "conv_curve_learned": "gelernte Kennlinie",
    "conv_curve_prior": "auf {prior}",
    "curve_title": "Wandlungs-Kennlinie",
    "curve_learned": "gelernte Kennlinie",
    "curve_applied": "angewendete Kennlinie",
    "curve_measured": "gemessen",
    "curve_maxload": "höchste Last",
    "curve_maxload_tip": "Die höchste Last, die diese Anlage je erreicht hat. Punkte darüber können nie Evidenz sammeln.",
    "curve_coverage_tip": "Anteil der erreichbaren Stützstellen, die die Messung bewegt hat.",
    "curve_unreachable": "nie erreicht",
    "curve_unreachable_band": "nie erreicht",
    "curve_collecting": "Lernen ist an und Messungen laufen ein — die Punkte wandern stufenlos mit ihrer Evidenz, es gibt keine Schwelle, an der sie springen.",
    "curve_blocked_derived": "Dieser Wechselrichter meldet einen rechnerischen AC-Wert (Eingang × fester Faktor), keine Messung — deshalb bleibt die Kennlinie beim Datenblatt. Das ist eine Eigenschaft der Hardware, kein Defekt. Mit einer echten Messsteckdose am AC-Ausgang, als gemessene AC-Leistung eingetragen, läuft das Lernen. Die Steckdose muss allein den Wechselrichter versorgen: alles andere daran verfälscht genau das Schwachlast-Ende, wo die Kennlinie am interessantesten ist.",
    "curve_blocked_other": "Lernen ist für diese Kennlinie ausgesetzt: {reason}. Die angewendete Kennlinie bleibt auf ihrem Prior.",
    "curve_tip_load": "Last",
    "curve_point_moving": "Messung liegt vor — der Punkt folgt ihr anteilig zu seiner Evidenz",
    "curve_point_nodata": "noch keine Messung bei dieser Last",
    "curve_point_blocked": "Messung verworfen — siehe Hinweis oben",
    "curve_point_unreachable": "nie erreicht (höchste Last {max} %) — bleibt beim Datenblatt und zählt nicht in den Reifegrad",
    "curve_prior_series": "Prior (Datenblatt)",
    "curve_coverage": "Stützstellen bewegt",
    "curve_delta": "Korrektur ggü. Prior (pp)",
    "curve_axis_load": "Last (Anteil der AC-Nennleistung)",
    "curve_axis_eta": "Wirkungsgrad",
    "curve_pending": "Noch keine gelernte Kennlinie für diese Gruppe. Der Kollektor sammelt: {usable} von {rows} Messpaaren sind zensurgeprüft. Stützstellen bewegen sich erst, wenn die Evidenz an einem Punkt reicht.",
    "curve_silent": "Für diese Gruppe sind Messpaare eingerichtet, aber es wird nichts gesammelt (0 Zeilen). Das ist kein Wartezustand — den konfigurierten Ausgangssensor prüfen.",
    "curve_not_collected": "Für diese Gruppe werden keine Messpaare gesammelt — kein Ausgangs-Sensor eingerichtet, oder das Lernen ist aus.",
    "curve_unsupported": "Diese Integrationsversion lernt keine Wandlungs-Kennlinien.",
    "curve_gathering": "Lernen ist an, bisher wurde keine Stützstelle bewegt — jeder Punkt hält noch seinen Prior. Evidenz je Punkt unten.",
    "curve_point_learned": "aus Messung gelernt",
    "curve_bins_missing": "Der Lern-Block enthält keine Stützstellen.",
    "curve_note": "Nicht gelernt werden: Intervalle am AC-Nennwert (dort folgt der Ausgang dem Eingang nicht mehr), unter 1 % Last (dort dominiert der Eigenverbrauch des Wechselrichters) und alles, was die Zensur als gedrosselt markiert hat. Gelernte Punkte sind auf ±5 Prozentpunkte um den Prior gedeckelt — ein Tag mit kaputtem Sensor kann die Kennlinie nicht umschreiben. Ganze Kennlinien werden verworfen, wenn die Messung zu flach ist, um eine zu sein.",
    "conv_curve_neutral": "ungewandelt (Ausgang = DC)",
    "conv_curve_fixed_factors": "feste Faktoren",
    "conv_fixed_factors_tip": "Feste Faktoren je Stufe auf DC angewendet (z. B. MPPT × Laden) — konfigurierte Werte, nicht gemessen.",
    "conv_neutral_note": "Keine Kennlinie konfiguriert — der Ausgang ist per Definition gleich DC, das ist kein gemessener 0-%-Verlust.",
    "conv_clipped": "Clipping {v} kWh",
    "conv_clipped_tip": "Energie über dem AC-Nennwert des Wechselrichters — eine Hardware-Kappung, kein Wandlungsverlust.",
    "conv_strip": "{out} / DC pro Stunde",
    "conv_low_dc": "zu wenig DC für einen sinnvollen Quotienten",
    "chain_title": "Prognosekette",
    "chain_physics": "Roh-Physik",
    "chain_shading": "Himmelskarte",
    "chain_model": "Strang-Modell",
    "chain_published": "veröffentlicht",
    "chain_measured": "gemessen",
    "chain_source_bias": "Source-Bias ×{v} — wurde upstream auf die Einstrahlung angewendet und steckt bereits in der Physik-Zahl. Kontext, kein Kettenglied.",
    "chain_discrepancy": "Physik × Verschattung × Modell ≠ veröffentlicht ({a} vs {b}). Die für diese Stunde veröffentlichten Faktoren ergeben multipliziert nicht den veröffentlichten Wert — bitte mit dem Diagnose-Download melden.",
    "chain_hour": "Stunde",
    "chain_no_hour": "Keine Prognosezeile für diese Stunde.",
    "chain_needs_string": "Diese Karte liest die Stundenkette, die nur Strang-Prognosesensoren publizieren.",
    "daily_title": "Day-Ahead vs. Ist",
    "daily_soll": "Prognose (Day-Ahead)",
    "daily_ist": "Ist",
    "daily_no_issue": "an dem Abend keine Prognose ausgegeben",
    "daily_provenance": "Day-Ahead = {entity}, Stand {date} {hour}:00 (Recorder-Statistik)",
    "daily_provenance_own": "Day-Ahead = ausgegeben {date} {hour}:00, aus der Aufzeichnung der Integration",
    "daily_window_sum": "{n} Tage: Prognose {soll} — Ist {ist}",
    "daily_wabs": "gewichteter |Fehler|",
    "v_overview": "Übersicht",
    "v_strings": "Stränge",
    "v_accuracy": "Genauigkeit",
    "v_nerd": "Nerd Dashboard",
    "s_today": "Heute",
    "s_tomorrow": "Morgen & Wetter",
    "s_savings": "Ersparnis",
    "s_groups": "Wechselrichter-Gruppen",
    "s_conversion": "Wandlung (AC / Speicher)",
    "nerd_conversion": "Wandlung",
    "conv_path": "Pfad",
    "conv_curve": "Kennlinie",
    "conv_stages": "Stufen",
    "conv_clipped_col": "Clipping",
    "conv_today_col": "Wandlung heute",
    "nerd_conv_evidence": "Lernen — Wandlungs-Evidenz",
    "conv_ev_stage": "Stufe",
    "conv_stage_inverter": "Wechselrichter",
    "conv_stage_mppt": "MPPT",
    "conv_ev_loading": "Sammel-Evidenz wird geladen…",
    "conv_ev_admin": "Die Wandlungs-Evidenz steckt in den Entry-Diagnosen, die einen Admin-Benutzer erfordern — daher hier leer.",
    "conv_ev_unavailable": "Die Entry-Diagnosen ließen sich nicht laden — ob die Sammlung läuft, lässt sich von hier nicht prüfen.",
    "conv_ev_none": "Die Integration sammelt noch keine Wandlungs-Evidenz — nichts zum Lernen konfiguriert.",
    "conv_ev_silent": "sammelt nichts",
    "conv_ev_note": "Verwertbar = von der Zensurprüfung freigegebene Messpaare; hinkt der Gesamtzahl stets bis zu einer Stunde hinterher — so arbeitet die Leitplanke. Eine 0 / 0-Zeile ist eingerichtet und sammelt trotzdem nichts. Realistisch sind ein paar hundert verwertbare Paare pro Stufe und Sonnentag; einen Fortschrittsbalken gibt es nicht, weil kein Schwellwert „genug“ definiert.",
    "s_daily": "Tag für Tag",
    "nerd_explain_title": "Was diese Zahlen bedeuten",
    "nerd_explain": "- **Faktor / n_eff** (Lern-Buckets): Der Faktor ist die gelernte Korrektur, mit der die Physik-Prognose multipliziert wird, als Prozent gezeigt — +5 % heißt „real kam 5 % mehr als gerechnet\" (Faktor 1,05, beim Überfahren). n_eff ist das wirksame Beweisgewicht dahinter (jüngere Stunden zählen mehr); kleine Werte heißen: noch vorläufig.\n- **Wetter × Tagesabschnitt**: Die Anlage lernt getrennt pro Wetterklasse und Tageszeit. „nie gesehen\" heißt genau das — dieses Wetter gab es zu dieser Tageszeit noch nicht, und auch das ist ein Befund. Der String × Tagesabschnitt-Layer kennt keine solche Schwelle: Seit Integration 1.20.1 wird jeder Bucket ab der ersten Beobachtung veröffentlicht und um so stärker gegen 1,00 gezogen, je dünner die Evidenz ist — halbe Stärke bei etwa zehn Beobachtungen, darunter fast neutral. Ein Faktor nahe 1,00 mit kleinem n ist dünn, nicht abgeschaltet.\n- **Source-Bias (Stunde × Horizont)**: der systematische Fehler der Wetterquelle je lokaler Stunde und Vorhersage-Horizont, als Faktor auf die Einstrahlung. *measured* = gegen einen echten Sensor gelernt; *nowcast* = nur gegen den Kurzfrist-Lauf der Quelle selbst — eine deutlich schwächere Aussage.\n- **Himmelskarte**: Das *Niveau* ist das Freisicht-Niveau des Strangs relativ zur Physik — 1,05 heißt: liefert 5 % über Physik, wo nichts im Weg ist. Wo Stränge genug gemeinsame Epochen haben, wird die Karte gegen die Geschwister-Stränge gefittet (*differenziell*); ein einzelner Strang fittet *absolut* und hat kein Niveau. Auf einer differenziellen Karte ist der Verlust jeder Zelle der Klartag-Verlust — was der Schatten an einem klaren Tag kostet; zur Laufzeit skaliert die Integration ihn mit dem Direktlicht-Anteil, ein trüber Tag verliert also fast nichts. n ist die beam-gewichtete Beobachtungszahl — kleiner als vor 1.18, was nicht „weniger Daten“ heißt.\n- **Nowcast**: die Prognose reagiert auf den eigenen Einstrahlungs-Sensor. Die gemessene Klarheit der letzten Viertelstunde wird in die kommenden Intervalle eingeblendet und mit einer Halbwertszeit, die vom Himmel abhängt, zur Anbieterprognose zurückgeführt — Reichweite zwei Stunden, vergangene Stunden bleiben unangetastet. Nachts und ohne Sensor ist „läuft nicht“ der Normalfall, und dann ist der Grund die interessantere Zahl. Nicht zu verwechseln mit *nowcast* in der Source-Bias-Tabelle, das sagt, woher der Bias gelernt wird.\n- **Erfassung / Zensur**: coverage ist der Anteil tatsächlich erfasster 5-Minuten-Intervalle — gezählt nur über Tageslichtstunden (PV Strings ≥ 1.16), eine nachts schlafende Quelle wird also nicht bestraft. *Untergrenze* markiert Stunden mit Abregelung — der echte Ertrag wäre höher gewesen, der Wert zählt nur als Minimum.\n- **Skip-Gründe**: wovon der Lernzyklus bewusst NICHT gelernt hat, und warum. Auf einer Anlage, die nichts lernt, ist diese Liste die ganze Diagnose.\n- **Lernreife**: Der Wetter-Balken ist das gehaltene Beweisgewicht über alle Wetter × Tagesabschnitt-Buckets, relativ zum Maximum, das ein Bucket je halten kann (das Lernen vergisst langsam, der Zähler sättigt — 100 % heißt „so gelernt wie es wird\", nicht „fertig\"; die Marke zeigt, wo Grün beginnt — der Punkt, der praktisch als fertig gelernt gilt). Der Verschattungs-Balken ist der Anteil des Jahres-Sonnenwegs, den jeder Strang schon gesehen hat; er wächst höchstens so schnell wie der Kalender.\n- **Wandlung (AC / Akkuladung)**: optional — erscheint, sobald eine Gruppe einen Ausgabepfad hat. AC ist Energie hinter dem Wechselrichter, bei Clipping am AC-Nennwert gedeckelt, aber nie an Regel- oder Rechtslimits; Akkuladung ist DC-Energie in den Speicher, deren Ausspeisezeitpunkt eine Regelentscheidung ist — die beiden werden nie addiert. Der Direktpfad rechnet mit einer lastabhängigen Kennlinie aus dem Datenblatt oder selbst eingetragen; wo gemessene DC/AC-Paare vorliegen und der Besitzer es einschaltet, korrigiert die Anlage diese Kennlinie mit der eigenen Messung. Der Speicherpfad rechnet mit festen Faktoren. „Ungewandelt“ heißt: keine Kennlinie konfiguriert, nicht 0 % Verlust gemessen.",
    "strategy_no_integration": "## PV Strings\nKeine PV-Strings-Entities gefunden. Zuerst die [PV-Strings-Integration](https://github.com/doccodyblue/ha-pvstrings) installieren und einrichten — dieses Dashboard baut sich aus ihren Sensoren.",
    "missing_card": "**{key}** wurde hier erwartet, aber es gibt keine solche Entity an diesem Gerät — sie wurde nicht stillschweigend weggelassen. Prüfen, ob die Integrationsversion sie publiziert oder ob die Entity deaktiviert ist.",
    "nerd_learning": "Lernen — Korrekturfaktoren",
    "nerd_plant_buckets": "Anlage: Wetter × Tagesabschnitt",
    "nerd_plant_hours": "Anlage: Wetter × Stunde",
    "nerd_bucket_missing": "nie gesehen",
    "cens_coverage": "Erfassung",
    "cens_curtailed": "Abregelung",
    "nerd_truth_measured": "gegen einen Messsensor gelernt",
    "nerd_truth_nowcast": "nur gegen den Kurzfrist-Lauf der Quelle selbst gelernt — eine deutlich schwächere Aussage",
    "nerd_collection": "Erfassung",
    "nerd_censoring": "Zensur-Split (heute)",
    "nerd_skips": "Skip-Gründe des Lernzyklus",
    "nerd_sky": "Himmelskarten",
    "maturity_title": "Lernreife",
    "maturity_weather": "Wetter-Korrektur",
    "maturity_shading": "Verschattung (Himmelskarten)",
    "maturity_buckets": "{seen} von {total} Buckets gesehen",
    "maturity_no_lat": "Standort unbekannt — keine Sonnenweg-Referenz",
    "kv_empty": "Attribut {path} ist leer oder fehlt auf {entity}",
    "factor": "Faktor",
    "weather_clear": "klar", "weather_partly_cloudy": "teils bewölkt",
    "weather_overcast": "bedeckt", "weather_rain": "Regen",
    "daypart_morning": "Vormittag", "daypart_midday": "Mittag",
    "daypart_afternoon": "Nachmittag",
    "hourly_legend_less": "weniger geliefert als gerechnet",
    "hourly_legend_more": "mehr geliefert",
    "hourly_note": "Eine Spalte pro Stunde, gezählt ab Sonnenmittag; die Uhrzeit ist der heutige Beginn dieser Stunde, gezeigt werden nur die heutigen Tagstunden. Die Stunden starteten mit dem Wert ihres alten Tagesabschnitts und lernen von dort einzeln weiter — innerhalb eines Abschnitts sehen sie anfangs gleich aus.",
    "hourly_note_rel": "Eine Spalte pro Stunde, in Stunden ab Sonnenmittag. Die Stunden starteten mit dem Wert ihres alten Tagesabschnitts und lernen von dort einzeln weiter — innerhalb eines Abschnitts sehen sie anfangs gleich aus.",
    "vk_measured": "gemessen", "vk_lower_bound": "Untergrenze",
    "vk_reconstructed": "rekonstruiert",
    "nerd_price": "Ersparnis — Herkunft",
    "price_win_today": "heute", "price_win_total": "gesamt",
    "price_recorded": "aufgezeichnet", "price_configured": "Festpreis",
    "price_hours": "Stunden mit Preis",
    "price_import": "⌀ Bezug", "price_export": "⌀ Einspeisung",
    "price_dropped": "Export ohne Deckung",
    "price_none_yet": "noch keine Stunde bepreist",
    "price_note": "Aufgezeichnet und Festpreis summieren sich auf die gelieferte Energie des Fensters. Festpreis ist alles, was kein aufgezeichneter Preis gedeckt hat — Stunden vor Einrichtung des Preissensors, Lücken darin, und die Seite, die dieses Tarifmodell bewertet, wo nur die andere aufgezeichnet war. Die Stundenzahl zählt Stunden mit irgendeinem aufgezeichneten Preis; maßgeblich ist die Energieaufteilung. Die Mittelpreise sind energiegewichtet, nicht arithmetisch: 0,21 ist ein Tarif, 21 ist ein Faktor-100-Fehler in der Sensoreinheit.",
    "price_dropped_note": "Export ohne Deckung ist Netzexport, den die Stränge in derselben Stunde nicht erzeugt haben — eine Akku-Entladung, ein zweiter Erzeuger hinter dem Zähler, ein falsch vorzeichenbehafteter Zähler. Es ist keine PV-Energie — und kein Verlust.",
    "price_tou_caveat": "Jahreshochrechnung bei zeitvariablem Tarif: gewichtet nach Einstrahlung, nicht nach Preis — im Winter sind es wenige, aber teure Stunden. Verschwindet, sobald ein volles Jahr aufgezeichnet ist.",
    "hp_title": "Day-Ahead-Fehler nach Stunde",
    "hp_forecast": "angesagt", "hp_actual": "gekommen",
    "hp_deviation": "Abweichung", "hp_days": "Tage",
    "hp_unit": "kWh/Tag",
    "hp_gap": "Differenz",
    "hp_sum_of": "{f} / {a} kWh aus {n} gescorten Tagen",
    "hp_days_scored_one": "1 Tag gescort", "hp_days_scored_many": "{n} Tage gescort",
    "hp_thin_one": "dünne Basis — 1 Tag", "hp_thin_many": "dünne Basis — {n} Tage",
    "hp_thin_hours_one": "1 Stunde auf dünner Basis", "hp_thin_hours_many": "{n} Stunden auf dünner Basis",
    "hp_too_high": "versprochen, nicht gekommen", "hp_too_low": "mehr gekommen",
    "s_status": "Status",
    "acc_title": "Genauigkeit",
    "acc_short": "Kurzfrist",
    "acc_short_note": "laufend aktualisiert",
    "acc_dayahead": "Day-Ahead",
    "acc_dayahead_note": "am Vorabend eingefroren",
    "acc_7d": "WMAPE 7 Tage", "acc_30d": "WMAPE 30 Tage",
    "acc_bias7": "Bias 7 Tage", "acc_bias30": "Bias 30 Tage",
    "acc_yesterday": "gestern",
    "acc_window": "{d} von {n} Tagen",
    "help_accuracy": "Der **Kurzfrist-Wert** darf sich tagsüber nachkorrigieren (und misst seit dem Nowcast bei Vorlauf 0 dessen Sensor-Korrektur mit); **Day-Ahead** ist am Vorabend eingefroren. Die beiden sind **erst vergleichbar, wenn beide Fenster voll sind** — die Tageszähler unter den Zahlen zeigen, wie weit jedes ist.\n\n**WMAPE** = gewichteter mittlerer absoluter Prozentfehler: die Summe aller Prognosefehler geteilt durch die Summe der echten Erträge — 10 % heißt, die Prognosen lagen in Summe 10 % daneben, wobei sonnige Stunden stärker zählen als Dämmerstunden. **Bias** ist der vorzeichenbehaftete Mittelwert pro Tag, in kWh: positiv heißt, die Prognose lag zu hoch.",
    "help_daily": "**Tag für Tag**: was am Vorabend angesagt war gegen das, was kam. Blau ist der Day-Ahead-Wert, wie er zur Ausgabestunde stand, Orange der gemessene Ertrag; ein schraffierter Stummel heißt, an dem Abend wurde keine Prognose ausgegeben, eine schraffierte Spitze, der Tag läuft noch. Die Chips schalten zwischen Anlage und Strängen um — dasselbe Diagramm, eines nach dem anderen.",
    "health_title": "Erfassung & Lernzyklus",
    "health_coverage": "Abdeckung",
    "health_write_errors": "Schreibfehler",
    "health_weather": "Wetterquelle",
    "health_ok": "ok",
    "health_learn": "Lernzyklus",
    "health_learn_v": "{used} von {hours} Stunden gelernt",
    "health_skipped": "übersprungen",
    "health_censored": "zensierte Stunden",
    "health_station": "Stationsluft",
    "health_station_v": "Temperatur {t} % · Wind {w} %",
    "health_station_none": "kein Stationssensor, oder vor Sonnenaufgang",
    "health_station_tip": "Anteil der heutigen Tageslicht-Intervalle, für die der konfigurierte Stationssensor einen Wert geliefert hat — die Kontrolle, dass gemessene Luft die Physik tatsächlich erreicht (PV Strings ≥ 1.24). Die Vorwärtsprognose rechnet weiter mit der Luft der Wetterquelle; Stationswerte fließen nur in die Rekonstruktion vergangener Stunden.",
    "health_all_good_one": "Alle {n} Stränge heute vollständig gemessen: {iv} Intervalle, {cov} % Abdeckung, nichts abgeregelt.",
    "health_all_good_many": "Alle {n} Stränge heute vollständig gemessen: {iv} Intervalle, {cov} % Abdeckung, nichts abgeregelt.",
    "health_no_cycle": "noch kein Lernzyklus aufgezeichnet",
    "health_learn_night": "nur Nachtstunden — nichts zu lernen",
    "health_learn_night_tip": "Der letzte Zyklus hatte nur Nachtstunden vor sich. Außerhalb des Tageslichts ist das der Normalfall, kein Fehler.",
    "health_per_string": "Abdeckung je Strang",
    "health_counters": "Kollektor-Zähler",
    "lbl_intervals_written": "Intervalle geschrieben",
    "lbl_events_seen": "Ereignisse gesehen",
    "lbl_write_errors": "Schreibfehler",
    "lbl_watchdog_ticks": "Watchdog-Ticks",
    "lbl_last_flush_duration_ms": "letzter Flush (ms)",
    "lbl_hours_materialised": "Stunden materialisiert",
    "lbl_observations_used": "Beobachtungen verwendet",
    "lbl_observations_skipped": "Beobachtungen übersprungen",
    "lbl_censored_hours": "zensierte Stunden",
    "lbl_bias_observations": "Bias-Beobachtungen",
    "lbl_shading_observations": "Verschattungs-Beobachtungen",
    "lbl_ghi_hours_rejected": "Einstrahlungs-Stunden verworfen",
    "lbl_reconstructed_intervals": "rekonstruierte Intervalle",
    "lbl_skipped_because": "übersprungen wegen",
    "nerd_strings_table": "Stränge: Offset und Tagesabschnitt",
    "nerd_strings_hours": "Stränge: Offset und Stunde",
    "col_offset": "Offset",
    "factor_tip_factor": "Faktor",
    "factor_tip_n": "Evidenz n_eff",
    "factor_thin": "dünne Evidenz — gegen 0 % gezogen",
    "factor_legend": "Korrektur, mit der die Physik-Prognose multipliziert wird; +7 % heißt: real kam 7 % mehr als gerechnet",
    "nerd_weather_source": "Wetterquelle",
    "nerd_sensor": "Einstrahlungssensor",
    "bias_title": "Source-Bias nach Stunde × Horizont",
    "bias_legend_high": "Quelle zu hoch",
    "bias_legend_low": "Quelle zu niedrig",
    "bias_thin": "dünn",
    "bias_horizon": "Horizont",
    "sky_cells_col": "Zellen",
    "sky_worst_col": "stärkster Schatten",
    "conv_stage_charge": "Laden",
    "nerd_conv_paths": "Wandlung — Pfade",
    "conv_ev_pairs_col": "verwertbar / gesamt",
    "th_title": "Zelltemperatur",
    "th_air": "Luft",
    "th_wind": "Wind",
    "th_cells": "Zellen",
    "th_effect": "Wirkung jetzt",
    "th_today": "heute",
    "th_plant_today": "Anlage heute",
    "th_by_heat": "durch Wärme",
    "th_ref": "25-°C-Referenz",
    "th_now": "jetzt",
    "th_no_rows": "heute noch keine Stunde mit Sonne — Zelltemperaturen gibt es nur, solange die Module Licht sehen",
    "th_note": "Modelliert, nicht gemessen: das Sandia-Zelltemperaturmodell je Montageart, gespeist mit Lufttemperatur, Wind und Einstrahlung in Modulebene aus der Prognose. Unter 25 °C Zellen gewinnen die Module, darüber verlieren sie — der Anteil steckt bereits in der Physik-Zahl, nie ein vierter Faktor.",
    "th_gain": "Gewinn durch Kälte",
    "th_loss": "Verlust durch Hitze",
    "chain_thermal": "Thermik ×{v} — {cell} °C Zellen bei {air} °C Luft, {wind} m/s Wind. Steckt bereits in der Physik-Zahl — Kontext, kein Kettenglied.",
    "help_factors": "**Korrekturfaktoren**: Mit ihnen wird die Physik-Prognose multipliziert — +7 % heißt, real kam 7 % mehr als gerechnet. Die Anlage lernt getrennt pro Wetterklasse und Tageszeit; *nie gesehen* heißt, dieses Wetter gab es zu dieser Tageszeit noch nicht — auch das ist ein Befund. Je Strang liegen ein Offset und eine Tagesabschnitt-Schicht darüber; dünne Buckets werden um so stärker gegen 0 % gezogen, je weniger Evidenz dahintersteht (halbe Stärke bei etwa zehn Beobachtungen). Eine Zelle zeigt beim Überfahren den rohen Faktor und seine Evidenz.",
    "help_bias": "**Source-Bias**: der systematische Fehler der Wetterquelle je lokaler Stunde und Vorhersage-Horizont, als Korrektur auf die Einstrahlung. Blau: die Quelle hat mehr angesagt als kam, die Prognose wird heruntergezogen; Orange: sie hat zu wenig angesagt. Blasse Zellen stehen auf dünner Evidenz. *Gemessen* heißt gegen einen echten Sensor gelernt; *nowcast* nur gegen den Kurzfrist-Lauf der Quelle selbst — eine deutlich schwächere Aussage.",
    "help_sky": "**Himmelskarten**: Das *Niveau* ist der Freisicht-Ertrag des Strangs relativ zur Physik — 1,05 liefert 5 % über Physik, wo nichts im Weg ist. Wo Stränge genug gemeinsame Epochen haben, wird die Karte gegen die Geschwister-Stränge gefittet (*differenziell*); ein einzelner Strang fittet *absolut* und hat kein Niveau. Der stärkste Schatten nennt den Himmelssektor (Azimut · Höhe) mit dem größten Klartag-Verlust.",
    "help_health": "**Erfassung**: Abdeckung ist der Anteil tatsächlich erfasster 5-Minuten-Intervalle, gezählt nur über Tageslichtstunden. **Lernzyklus**: was der letzte Zyklus verwendet und was er bewusst übersprungen hat, samt Grund — auf einer Anlage, die nichts lernt, ist diese Liste die ganze Diagnose. Nachtstunden zählen nicht als übersprungen: aus ihnen gibt es nichts zu lernen. **Zensierte** Stunden sind Untergrenzen (der Wechselrichter war abgeregelt, der echte Ertrag wäre höher gewesen). Tabellen öffnen sich nur, wo ein Wert abweicht.",
    "help_maturity": "**Lernreife**: Der Wetter-Balken ist das gehaltene Beweisgewicht über alle Wetter × Tagesabschnitt-Buckets, relativ zum Maximum, das ein Bucket halten kann — das Lernen vergisst langsam, 100 % heißt also „so gelernt wie es wird“, nicht „fertig“; die Marke zeigt, wo Grün beginnt. Der Verschattungs-Balken ist der Anteil des Jahres-Sonnenwegs, den jeder Strang schon gesehen hat; er wächst höchstens so schnell wie der Kalender.",
    "help_thermal": "**Zelltemperatur**: je Strang modelliert aus Lufttemperatur, Wind und Einstrahlung in Modulebene der Prognose — nicht gemessen. Zellen über 25 °C verlieren Leistung, darunter gewinnen sie; die Wirkung steckt bereits in der Physik-Zahl der Prognosekette, diese Karte macht sie nur sichtbar. Eine flache, hinterlüftungslose Montage läuft bei gleicher Luft heißer als ein freies Gestell.",
    "help_conversion": "**Wandlung**: AC ist Energie hinter dem Wechselrichter, bei Clipping am AC-Nennwert gedeckelt, aber nie an Regel- oder Rechtslimits; Akkuladung ist DC-Energie in den Speicher — die beiden werden nie addiert. Kennlinien sind konfiguriert (Datenblatt oder selbst eingetragen); wo gemessene DC/AC-Paare vorliegen und das Lernen an ist, korrigiert die Anlage die Kennlinie mit der eigenen Messung. „Ungewandelt“ heißt: keine Kennlinie konfiguriert, nicht 0 % Verlust gemessen.",
    "help_hp": "**Day-Ahead-Fehler nach Stunde**: dieselben gescorten Paare, aus denen die 30-Tage-Zahl gebildet wird, nach lokaler Stunde gefaltet, in kWh pro gescorten Tag. Der Streifen ist die Differenz zwischen versprochen und gekommen — über der Linie hat die Prognose mehr versprochen als kam. Damit liest man, *wo im Tag* die Prognose schwach ist und was das kostet; der Prozentwert je Stunde steht im Tooltip. Gedimmte Stunden stehen auf weniger als drei Tagen.",
    "help_nowcast": "**Nowcast**: die Prognose reagiert auf den eigenen Einstrahlungs-Sensor. Die gemessene Klarheit der letzten Viertelstunde wird in die kommenden Intervalle eingeblendet und mit einer Halbwertszeit, die vom Himmel abhängt, zur Anbieterprognose zurückgeführt — Reichweite zwei Stunden, vergangene Stunden bleiben unangetastet. Die blaue Marke ist, was die Wetterquelle für jetzt erwartet, auf derselben Skala — der Abstand zwischen beiden ist das, was der Nowcast korrigiert. Nachts und ohne Sensor ist „läuft nicht“ der Normalfall; dann ist der Grund die interessantere Zahl.",
    "hp_no_hours": "Noch keine gescorte Stunde. Das Profil füllt sich ab dem ersten vollständigen Tag — es wartet nicht auf die Genauigkeitszahlen.",
    // Historie (Services get_day / get_weeks)
    "hist_prev": "früher", "hist_next": "später", "hist_pick": "Tag auswählen",
    "hist_failed": "Historie ließ sich nicht laden: {error}",
    "hist_needs_service": "Braucht den Service {service} — eine neuere PV-Strings-Integration.",
    "hist_no_data": "Für diesen Tag ist nichts aufgezeichnet.",
    "hist_no_intervals": "Fünf-Minuten-Werte reichen nur bis {date} zurück — Stundenwerte gezeigt.",
    "hist_day_ahead": "Vorabend",
    "hist_day_ahead_gone": "Die Vorabend-Prognose wird nur etwa fünf Wochen aufbewahrt; dieser Tag zeigt die letzte Prognose vor jeder Stunde.",
    "hist_hero_actual": "Ist an dem Tag", "hist_hero_forecast": "Prognose an dem Tag",
    "hist_issued_earlier": "aus dem Lauf von {when} — der Abendlauf fehlte",
    "week_short": "KW {n}", "week_running": "Woche läuft noch",
    "week_backfilled": "nachgerechnet",
    "week_backfilled_tip": "Nachträglich aus den damals veröffentlichten Prognosen gerechnet; einen Modellstand aus dieser Woche gibt es nicht.",
    "hp_sel_live": "30 Tage",
    "hp_note_week": "Dieselben Day-Ahead-Paare, nur für diese Woche, nach lokaler Stunde gefaltet. Beide Hälften sind kWh pro gescorten Tag, wie beim Monat; über der Linie hat die Stunde mehr versprochen als kam.",
    // Lernfortschritt
    "learn_title": "Lernfortschritt",
    "learn_no_weeks": "noch keine Woche aufgezeichnet",
    "learn_no_baseline": "noch keine Woche mit Vergleich gegen die Prognose ohne Lernen",
    "learn_needs_hourly": "braucht den stündlichen Fehler auf den Wochen (PV Strings 1.25.2 oder neuer). Früher aufgezeichnete Wochen tragen einen Tageswert, in dem sich ein Vormittags- und ein Nachmittagsfehler aufheben — damit kann dieser Vergleich nicht rechnen.",
    "learn_hero_less_one": "weniger Prognosefehler als ohne Lernen · 1 verglichene Woche",
    "learn_hero_less_many": "weniger Prognosefehler als ohne Lernen · {n} verglichene Wochen",
    "learn_hero_more_one": "mehr Prognosefehler als ohne Lernen · 1 verglichene Woche",
    "learn_hero_more_many": "mehr Prognosefehler als ohne Lernen · {n} verglichene Wochen",
    "learn_hero_equal_one": "kein Unterschied zur Prognose ohne Lernen · 1 verglichene Woche",
    "learn_hero_equal_many": "kein Unterschied zur Prognose ohne Lernen · {n} verglichene Wochen",
    "learn_hero_wmape": "Day-Ahead-Fehler in KW {week} · ohne Lernen {base} %",
    "learn_hero_wmape_only": "Day-Ahead-Fehler in KW {week}",
    "learn_hero_thin": "noch kein Urteil möglich — {have} von {need} vollständigen Wochen gegen ausgeschaltetes Lernen verglichen",
    "learn_leg_gain": "Lernen half",
    "learn_leg_loss": "ohne besser",
    "learn_leg_excluded": "nicht mitgezählt",
    "learn_note": "Je Woche: der Day-Ahead-Prognosefehler dieser Integration gegen dieselbe Integration mit ausgeschaltetem Lernen — derselbe Wetterlauf, dieselbe Geometrie, dieselben Stunden, je Stunde summiert. Über der Linie hat das Lernen in dieser Woche geholfen, darunter wäre die Woche ohne besser gelaufen. Schraffierte Wochen haben einen Wert, der sich nicht aufsummieren lässt; die Gesamtzahl nennt, auf wie vielen Wochen sie steht.",
    "learn_tip_with": "Fehler mit Lernen", "learn_tip_without": "Fehler ohne Lernen",
    "learn_tip_gain": "vermiedener Fehler", "learn_tip_days": "verglichene Tage",
    "learn_tip_no_baseline": "für diese Woche kein Vergleich",
    "learn_tip_running": "läuft noch — zählt, sobald die Woche abgeschlossen ist",
    "learn_tip_replayed": "mit heutigem Code nachgerechnet: also nicht nur Lernen an und aus, sondern auch zwei Code-Stände. Nicht mitgezählt.",
    "learn_tip_mixed": "ein Teil dieser Woche wurde live verglichen, ein Teil mit heutigem Code nachgerechnet. Nicht mitgezählt.",
    "learn_tip_unknown": "aufgezeichnet, bevor die Integration notierte, woher ein Vergleich stammt. Nicht mitgezählt.",
    "help_learning": "**Ein Balken pro Woche: was das Lernen gekostet oder gespart hat.** Der Day-Ahead-Prognosefehler dieser Integration gegen dieselbe Integration mit ausgeschaltetem Lernen — derselbe Wetterlauf, dieselbe Geometrie, dieselben Stunden. Nicht gegenüber reiner Physik: die gelernte Verschattungskarte und die Quellen-Korrektur zählen auch als Lernen. Der Fehler wird **je Stunde** summiert, weil sich Vormittags- und Nachmittagsfehler in einer Tagessumme aufheben und eine Prognose ohne multiplikative Korrektur das eher tut — der Vergleich würde sonst zum Geschenk an die Baseline. Eine Woche zählt nur, wenn sie abgeschlossen ist und ihr Vergleich damals mitgeschrieben wurde. Nachträglich nachgerechnete Wochen, teils nachgerechnete, solche aus der Zeit vor dieser Herkunftsnotiz und die laufende Woche behalten ihren schraffierten Balken und bleiben aus der Summe: Sie vergleichen auch zwei Code-Stände, jede spätere Korrektur würde also gegen das Lernen zählen. Die Hauptzahl wartet auf drei gezählte Wochen — eine Woche ist eine Wetterstichprobe, kein Urteil — und nennt immer, auf wie vielen Wochen sie steht.",
    "hp_note": "Wo im Tag die Day-Ahead-Prognose Energie liegen lässt, aus denselben Paaren, aus denen die 30-Tage-Zahl gebildet wird. Beide Hälften sind kWh pro gescorten Tag — jede Stunde geteilt durch ihre eigene Tageszahl, damit eine Woche und der Monat nebeneinander bestehen; die Summe dahinter steht im Tooltip. Der Streifen ist die Differenz: über der Linie hat die Stunde mehr versprochen als kam, darunter war die Realität besser. Eine Stunde mit kaum Energie zeigt kaum Differenz — das ist das ehrliche Bild; ihr Prozentwert steht im Tooltip, wo eine Frage ans Modell ihn findet.",
    /* i18n-de-end */
  },
};

function langOf(hass) {
  const l = (hass?.locale?.language ?? hass?.language ?? "en").toLowerCase();
  return l.startsWith("de") ? "de" : "en";
}

function t(hass, key, vars) {
  const lang = typeof hass === "string" ? hass : langOf(hass);
  let s = STR[lang]?.[key] ?? STR.en[key] ?? key;
  if (vars) for (const [k, v] of Object.entries(vars)) {
    s = s.replaceAll(`{${k}}`, String(v));
  }
  return s;
}

// Singular/plural for the few counted strings that can legitimately be 1
// (a single thin hour, a plant one day old). German and English agree on
// where the split is, so one suffix pair covers both dictionaries.
function tn(hass, key, n, vars) {
  return t(hass, `${key}_${n === 1 ? "one" : "many"}`, { n, ...vars });
}

/* ============================ SECTION: THEME ============================= */

// Series identity, consistent across all cards (validated with the dataviz
// palette validator against HA's default surfaces #ffffff / #1c1c1c):
//   model quantities = blue, its earlier stage (unshaded) = light ordinal
//   step of the same hue, measurement = orange, loss = violet sequential.
// Loss ramp = shadow ramp: light means clear sky, dark means shadow — the
// iconic mapping (shading IS darkness) needs no legend to be learned. The
// direction is the SAME in both themes (a sunny sky is bright, also in
// night mode).
// The hue is violet and genuinely saturated, not a grey with a cast: a
// near-neutral dark end sank into the card background on dark themes, which
// put the deepest shadow within a whisker of an unobserved cell — the one
// collision this map must never have. Violet also sits opposite the orange
// sun marker and clear of the model blue, so all three read apart.
const LOSS_RAMP_LIGHT = ["#f4f0fd", "#e5dbfa", "#d1bef5", "#b79ced",
  "#9a77e0", "#7d55cd", "#6139b0", "#48268c"];
const LOSS_RAMP_DARK = ["#f0ebfd", "#ded2fa", "#c6b2f3", "#ab8ee9",
  "#8f6ada", "#744ec5", "#5c3aa8", "#472c86"];

const BASE_CSS = `
  :host {
    --pvs-model: #2a78d6;
    --pvs-model-ghost: #a8c9f2;
    --pvs-measure: #eb6834;
    --pvs-sun: #eda100;
    --pvs-unobserved: color-mix(in srgb, var(--secondary-text-color, #727272) 16%, var(--card-background-color, #fff));
    --pvs-hairline: color-mix(in srgb, var(--primary-text-color, #212121) 9%, transparent);
    --pvs-cell-stroke: color-mix(in srgb, var(--primary-text-color, #212121) 16%, transparent);
    --pvs-chip-bg: color-mix(in srgb, var(--primary-text-color, #212121) 5%, transparent);
    --pvs-mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    /* categorical series (one line per string): model blue and measure
       orange first, then hues that stay clear of both and of the violet
       shadow ramp */
    --pvs-cat-0: #2a78d6; --pvs-cat-1: #eb6834; --pvs-cat-2: #7d55cd;
    --pvs-cat-3: #1f9e8c; --pvs-cat-4: #c49a1a; --pvs-cat-5: #c2185b;
    ${LOSS_RAMP_LIGHT.map((c, i) => `--pvs-loss-${i}: ${c};`).join("\n    ")}
  }
  :host([dark]) {
    --pvs-model: #3987e5;
    --pvs-model-ghost: #21548f;
    --pvs-measure: #d95926;
    --pvs-sun: #eda100;
    --pvs-cat-0: #3987e5; --pvs-cat-1: #e0692f; --pvs-cat-2: #9a77e0;
    --pvs-cat-3: #2db8a3; --pvs-cat-4: #d9b03a; --pvs-cat-5: #e04d8b;
    ${LOSS_RAMP_DARK.map((c, i) => `--pvs-loss-${i}: ${c};`).join("\n    ")}
  }
  * { box-sizing: border-box; }
  ha-card { padding: 16px; overflow: hidden; position: relative; }
  .pvs-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
  .pvs-title { font-size: 15px; font-weight: 600; letter-spacing: 0.2px; color: var(--primary-text-color); margin-right: auto; }
  .pvs-sub { font-size: 11px; color: var(--secondary-text-color); }
  .pvs-num { font-family: var(--pvs-mono); font-variant-numeric: tabular-nums; }
  .pvs-chip {
    display: inline-flex; align-items: center; gap: 5px;
    font-size: 11px; line-height: 1; padding: 5px 8px; border-radius: 6px;
    background: var(--pvs-chip-bg); color: var(--secondary-text-color);
    border: 1px solid transparent; white-space: nowrap;
  }
  .pvs-chip.clickable { cursor: pointer; transition: border-color 150ms; }
  .pvs-chip.clickable:hover { border-color: var(--pvs-hairline); }
  .pvs-chip .v { font-family: var(--pvs-mono); font-variant-numeric: tabular-nums; color: var(--primary-text-color); font-size: 12px; }
  .pvs-chip.warn { background: color-mix(in srgb, var(--warning-color, #ffa600) 14%, transparent); color: var(--primary-text-color); }
  .pvs-chip.warn ha-icon, .pvs-chip.warn .ico { color: var(--warning-color, #ffa600); }
  .pvs-withheld {
    display: inline-flex; align-items: center; gap: 5px;
    font-size: 11px; line-height: 1.3; padding: 4px 8px; border-radius: 6px;
    border: 1px dashed color-mix(in srgb, var(--secondary-text-color, #727272) 55%, transparent);
    color: var(--secondary-text-color); background: transparent;
  }
  .pvs-problem {
    display: flex; gap: 12px; padding: 14px;
    border-radius: 8px; border: 1px solid color-mix(in srgb, var(--warning-color, #ffa600) 40%, transparent);
    background: color-mix(in srgb, var(--warning-color, #ffa600) 7%, transparent);
    color: var(--primary-text-color); font-size: 12.5px; line-height: 1.5;
  }
  .pvs-problem .ico { flex: none; font-size: 18px; }
  .pvs-problem code { font-family: var(--pvs-mono); font-size: 11.5px; background: var(--pvs-chip-bg); padding: 1px 4px; border-radius: 4px; }
  .pvs-problem .ent { color: var(--secondary-text-color); font-size: 11.5px; margin-top: 4px; }
  .pvs-legend { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 10px; font-size: 11px; color: var(--secondary-text-color); align-items: center; }
  .pvs-legend .it { display: inline-flex; align-items: center; gap: 6px; }
  .pvs-legend .sw { width: 10px; height: 10px; border-radius: 2px; flex: none; }
  .pvs-tip {
    position: absolute; z-index: 5; pointer-events: none;
    background: var(--card-background-color, #fff);
    border: 1px solid var(--pvs-hairline);
    box-shadow: 0 4px 16px rgba(0,0,0,0.18);
    border-radius: 8px; padding: 9px 11px; font-size: 11.5px; line-height: 1.55;
    color: var(--primary-text-color); width: max-content; max-width: 260px;
    opacity: 0; transform: translateY(3px); transition: opacity 120ms, transform 120ms;
  }
  .pvs-tip.on { opacity: 1; transform: translateY(0); }
  .pvs-tip .h { font-weight: 600; margin-bottom: 3px; }
  /* the "?" that replaces the explainer footer: one per card head, the
     text opens inline under it so it stays next to the numbers it explains */
  .pvs-helpbox {
    font-size: 12px; line-height: 1.55; color: var(--primary-text-color);
    background: var(--pvs-chip-bg); border-left: 3px solid var(--pvs-model);
    border-radius: 6px; padding: 9px 12px; margin: 0 0 12px;
  }
  .pvs-helpbox b { font-weight: 600; }
  .pvs-help {
    display: inline-flex; align-items: center; justify-content: center;
    width: 18px; height: 18px; border-radius: 50%; flex: none;
    font-size: 11px; font-weight: 600; line-height: 1; cursor: help;
    color: var(--secondary-text-color); border: 1px solid var(--pvs-hairline);
    transition: color 150ms, border-color 150ms; user-select: none;
  }
  .pvs-help:hover, .pvs-help.on { color: var(--primary-text-color); border-color: var(--secondary-text-color); }
  .pvs-tip .r { display: flex; justify-content: space-between; gap: 14px; }
  .pvs-tip .r .k { color: var(--secondary-text-color); }
  .pvs-tip .r .v { font-family: var(--pvs-mono); font-variant-numeric: tabular-nums; }
  .pvs-click { cursor: pointer; }
  /* history stepper: ‹ label › — a past day or week. On the day stepper the
     label also opens the browser's calendar, for more than a few steps */
  .pvs-step { display: inline-flex; align-items: center; gap: 2px; font-size: 11px;
    border: 1px solid var(--pvs-hairline); border-radius: 6px; background: var(--pvs-chip-bg); }
  .pvs-step button { all: unset; cursor: pointer; padding: 3px 8px; line-height: 1; font-size: 14px;
    color: var(--primary-text-color); border-radius: 5px; }
  .pvs-step button:hover:not([disabled]) { background: var(--pvs-hairline); }
  .pvs-step button:focus-visible { outline: 2px solid var(--pvs-model); outline-offset: -2px; }
  .pvs-step button[disabled] { cursor: default; opacity: 0.3; }
  .pvs-step .lbl { padding: 0 4px; min-width: 64px; text-align: center; white-space: nowrap;
    color: var(--secondary-text-color); font-variant-numeric: tabular-nums; font-size: 11px; }
  .pvs-step .lbl.pick { position: relative; padding: 3px 4px; cursor: pointer; }
  .pvs-step .lbl.pick:hover { text-decoration: underline dotted; text-underline-offset: 3px; }
  /* font-size 16px: iOS zooms the page into any smaller field it focuses */
  .pvs-step .lbl.pick input { position: absolute; inset: 0; width: 100%; height: 100%; min-width: 0;
    box-sizing: border-box; margin: 0; padding: 0; border: 0; opacity: 0; cursor: pointer; font-size: 16px; }
  .pvs-step.past { border-color: var(--pvs-model); }
  .pvs-step.past .lbl { color: var(--primary-text-color); }
  svg text { fill: var(--secondary-text-color); font-size: 10px; font-family: inherit; }
  svg .axis text { font-family: var(--pvs-mono); font-variant-numeric: tabular-nums; }
  svg .grid { stroke: var(--pvs-hairline); stroke-width: 1; }
  @media (prefers-reduced-motion: reduce) {
    * { transition: none !important; animation: none !important; }
  }
`;

// One shared SVG hatch pattern factory: 45° lines, tone-on-tone.
// Returns the <pattern> markup; reference it as fill="url(#<id>)".
function hatchPattern(id, stroke = "var(--secondary-text-color)") {
  return `<pattern id="${id}" width="6" height="6" patternTransform="rotate(45)"
    patternUnits="userSpaceOnUse">
    <line x1="0" y="0" x2="0" y2="6" stroke="${stroke}" stroke-width="1.1" opacity="0.62"/>
  </pattern>`;
}

function lossColor(lossPct) {
  // loss 0..95 -> ramp index 0..7
  const i = Math.max(0, Math.min(7, Math.floor((lossPct / 95) * 8)));
  return `var(--pvs-loss-${i})`;
}

function isDarkTheme(hass, el) {
  if (hass?.themes?.darkMode !== undefined) return !!hass.themes.darkMode;
  // Fallback: luminance of the resolved card background.
  try {
    const bg = getComputedStyle(el).getPropertyValue("--card-background-color").trim();
    const m = bg.match(/^#([0-9a-f]{6})$/i);
    if (m) {
      const n = parseInt(m[1], 16);
      const lum = (0.2126 * (n >> 16 & 255) + 0.7152 * (n >> 8 & 255) + 0.0722 * (n & 255)) / 255;
      return lum < 0.4;
    }
  } catch (_) { /* keep default */ }
  return false;
}

/* ============================ SECTION: FMT =============================== */

const _fmtCache = new Map();
function _intl(kind, lang, opts) {
  const key = kind + lang + JSON.stringify(opts);
  let f = _fmtCache.get(key);
  if (!f) {
    f = kind === "n" ? new Intl.NumberFormat(lang, opts) : new Intl.DateTimeFormat(lang, opts);
    _fmtCache.set(key, f);
  }
  return f;
}

function fmtNum(hass, v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return _intl("n", langOf(hass), { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v);
}
function fmtKwh(hass, v, digits = 2) {
  return v == null ? "–" : `${fmtNum(hass, v, digits)} kWh`;
}
function fmtPct(hass, v, digits = 0) {
  return v == null ? "–" : `${fmtNum(hass, v, digits)} %`;
}
function fmtSigned(hass, v, digits = 2) {
  if (v == null) return "–";
  // a value that rounds to zero carries no sign: "−0.00" reads as a finding
  const r = Number(v.toFixed(digits));
  return (r > 0 ? "+" : r < 0 ? "−" : "±") + fmtNum(hass, Math.abs(v), digits);
}

// The ONLY sanctioned way to bucket a UTC timestamp into HA-local calendar
// parts (DST-safe). Never use ts % 86400.
function localParts(hass, dateOrMs) {
  const d = typeof dateOrMs === "number" ? new Date(dateOrMs) : dateOrMs;
  const tz = hass?.config?.time_zone;
  const f = _intl("d", "en-CA", {
    timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23", weekday: "short",
  });
  const p = {};
  for (const part of f.formatToParts(d)) p[part.type] = part.value;
  return {
    dayKey: `${p.year}-${p.month}-${p.day}`,
    hour: parseInt(p.hour, 10),
    minute: parseInt(p.minute, 10),
    weekday: p.weekday,
  };
}

function fmtWeekday(hass, ms) {
  return _intl("d", langOf(hass), { timeZone: hass?.config?.time_zone, weekday: "short" }).format(new Date(ms));
}
function fmtDayShort(hass, ms) {
  return _intl("d", langOf(hass), { timeZone: hass?.config?.time_zone, day: "numeric", month: "numeric" }).format(new Date(ms));
}
function fmtHour(hass, ms) {
  return _intl("d", langOf(hass), { timeZone: hass?.config?.time_zone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(ms));
}

// Start of the HA-local day containing `ms`, minus `daysBack` days, as epoch ms.
// Walks backwards hour by hour from a coarse guess — cheap and DST-correct.
function localMidnightMs(hass, ms, daysBack = 0) {
  let target = ms - daysBack * 86400000;
  const dayKey = localParts(hass, target).dayKey;
  // coarse: jump to earlier until day changes, then binary refine by hour
  let lo = target - 26 * 3600000, hi = target;
  while (hi - lo > 60000) {
    const mid = Math.floor((lo + hi) / 2);
    if (localParts(hass, mid).dayKey === dayKey) hi = mid; else lo = mid;
  }
  return hi - (hi % 60000);
}

/* ============================ SECTION: DATA ============================== */

// Module-level cache with in-flight dedupe. TTL depends on whether the
// window touches "now".
const _wsCache = new Map();
function cachedWS(key, ttlMs, factory) {
  const hit = _wsCache.get(key);
  const now = Date.now();
  if (hit && now - hit.t < ttlMs) return hit.promise;
  const promise = factory().catch((e) => { _wsCache.delete(key); throw e; });
  _wsCache.set(key, { t: now, promise });
  return promise;
}

// ---- registry model -------------------------------------------------------

async function getRegistryModel(hass) {
  return cachedWS("registry", 5 * 60000, async () => {
    const [entities, devices] = await Promise.all([
      hass.callWS({ type: "config/entity_registry/list" }),
      hass.callWS({ type: "config/device_registry/list" }),
    ]);
    const devById = new Map(devices.map((d) => [d.id, d]));
    const model = { plants: [], strings: [], groups: [], byEntityId: new Map() };
    const nodeByDevice = new Map();

    const knownKeys = new Set([...PLANT_KEYS, ...STRING_KEYS, ...GROUP_KEYS]);
    const keyFromUnique = (uid, level) => {
      // unique_id = <entry>_<key> (plant) or <entry>_<sub>_<key> (string/group)
      const base = level === "plant" ? PLANT_KEYS
        : level === "group" ? GROUP_KEYS : STRING_KEYS;
      for (const k of base) {
        const plain = k.replace(/^string_|^group_/, "");
        if (uid.endsWith(`_${plain}`) || uid.endsWith(`_${k}`)) return k;
      }
      return null;
    };

    for (const e of entities) {
      if (e.platform !== "pvstrings" || e.disabled_by) continue;
      const dev = devById.get(e.device_id);
      if (!dev) continue;
      const level = dev.model === "PV plant" ? "plant"
        : dev.model === "PV string" ? "string"
        : dev.model === "Curtailment group" ? "group" : null;
      if (!level) continue;
      let node = nodeByDevice.get(dev.id);
      if (!node) {
        node = {
          deviceId: dev.id,
          // config entry id — the stable handle for the entry-level
          // diagnostics download (data.conversion_evidence lives there)
          entryId: dev.primary_config_entry ?? dev.config_entries?.[0]
            ?? dev.config_entry_id ?? null,
          // subentry ULID: the scope_id the diagnostics key their
          // per-group/per-string blocks by (identifier = <entry>_<scope>)
          scopeId: (() => {
            for (const id of dev.identifiers ?? []) {
              if (id[0] !== "pvstrings") continue;
              const parts = String(id[1]).split("_");
              if (parts.length >= 2) return parts.slice(1).join("_");
            }
            return null;
          })(),
          name: dev.name_by_user || dev.name,
          viaDeviceId: dev.via_device_id ?? null, byKey: {},
        };
        nodeByDevice.set(dev.id, node);
        model[level === "plant" ? "plants" : level === "string" ? "strings" : "groups"].push(node);
      }
      let tk = e.translation_key;
      if (level === "string" && tk && !tk.startsWith("string_")) tk = `string_${tk}`;
      if (level === "group" && tk && !tk.startsWith("group_")) tk = `group_${tk}`;
      if (!tk || !knownKeys.has(tk)) tk = keyFromUnique(e.unique_id ?? "", level) ?? tk;
      if (tk) node.byKey[tk] = e.entity_id;
      model.byEntityId.set(e.entity_id, { node, level, key: tk });
    }
    // link strings/groups to their plant
    for (const list of [model.strings, model.groups]) {
      for (const n of list) {
        n.plant = model.plants.find((p) => p.deviceId === n.viaDeviceId)
          ?? model.plants[0] ?? null;
      }
    }
    // stable order: registry order is creation order; sort by name for humans
    model.strings.sort((a, b) => a.name.localeCompare(b.name));
    return model;
  });
}

// Same-device sibling entity by translation_key.
async function sibling(hass, entityId, key) {
  const m = await getRegistryModel(hass);
  return m.byEntityId.get(entityId)?.node?.byKey?.[key] ?? null;
}
// Hop to the plant device (from a string/group entity; identity for plant).
async function plantSibling(hass, entityId, key) {
  const m = await getRegistryModel(hass);
  const info = m.byEntityId.get(entityId);
  if (!info) return null;
  const plant = info.level === "plant" ? info.node : info.node.plant;
  return plant?.byKey?.[key] ?? null;
}

// string/group scope id (subentry ULID) -> display name, via the device
// identifiers. The learning and collector blocks key their rows by scope id.
function stringNamesById(hass) {
  return cachedWS("string-names", 5 * 60000, async () => {
    const names = new Map();
    const devices = await hass.callWS({ type: "config/device_registry/list" });
    for (const d of devices) {
      if (d.model !== "PV string" && d.model !== "Curtailment group") continue;
      for (const ident of d.identifiers ?? []) {
        if (ident[0] !== "pvstrings") continue;
        const parts = String(ident[1]).split("_");
        if (parts.length >= 2) names.set(parts.slice(1).join("_"), d.name_by_user || d.name);
      }
    }
    return names;
  });
}

// ---- diagnostics ----------------------------------------------------------

// Entry-level diagnostics download. Since HA 2024-ish this is an HTTP
// endpoint, not a WS command, and it is admin-only: a non-admin viewer gets
// 401/403, which the card shows as a note — never as a contract violation.
// The collector's conversion evidence (learning pairs rows/usable per stage)
// lives under data.conversion_evidence; the block is absent until the
// integration collects, which is "not configured", not an error.
async function entryDiagnostics(hass, entryId) {
  if (!entryId) return { error: "no_entry" };
  try {
    return await cachedWS(`diag|${entryId}`, 5 * 60000, async () => {
      try {
        const res = await hass.fetchWithAuth(
          `/api/diagnostics/config_entry/${entryId}`);
        if (res.status === 401 || res.status === 403) return { error: "admin" };
        if (!res.ok) return { error: "unavailable" };
        return { data: (await res.json())?.data ?? null };
      } catch (_) { return { error: "unavailable" }; }
    });
  } catch (_) { return { error: "unavailable" }; }
}

async function conversionEvidence(hass, entryId) {
  const d = await entryDiagnostics(hass, entryId);
  return d.error ? d : { evidence: d.data?.conversion_evidence ?? null };
}

// model.conversion_curves holds the learning regions the integration has
// actually built. A region appearing there means learning is on; a region
// MISSING there does not prove it is off — observed live: learning on, pairs
// collecting, still no region. So the absence is reported as "no curve yet",
// with the collector's own evidence as the signal for whether it is working.
async function conversionState(hass, entryId, scope) {
  const d = await entryDiagnostics(hass, entryId);
  if (d.error) return d;
  const model = d.data?.model;
  if (!model || !("conversion_curves" in model)) return { error: "unsupported" };
  const curves = model.conversion_curves ?? {};
  const hit = scope ? Object.entries(curves)
    .find(([k]) => k === scope || k.startsWith(`${scope}|`))?.[1] : null;
  if (hit) return { learning: hit };
  const mine = Object.entries(d.data?.conversion_evidence ?? {})
    .filter(([k]) => scope && (k === scope || k.startsWith(`${scope}|`)));
  return {
    error: "pending",
    collecting: mine.length > 0,
    rows: mine.reduce((s, [, v]) => s + (v?.rows ?? 0), 0),
    usable: mine.reduce((s, [, v]) => s + (v?.usable ?? 0), 0),
  };
}

// ---- statistics -----------------------------------------------------------

const _tsNorm = (v) => (typeof v === "number" ? (v > 1e12 ? v : v * 1000)
  : new Date(v).getTime());

async function wsStats(hass, { ids, startISO, endISO, period, types }) {
  const key = `stats|${ids.join(",")}|${startISO}|${endISO}|${period}|${types}`;
  const touchesNow = !endISO || new Date(endISO).getTime() > Date.now() - 3600000;
  return cachedWS(key, touchesNow ? 5 * 60000 : 30 * 60000, async () => {
    const res = await hass.callWS({
      type: "recorder/statistics_during_period",
      start_time: startISO,
      ...(endISO ? { end_time: endISO } : {}),
      statistic_ids: ids,
      period,
      types,
      units: { energy: "kWh" },
    });
    const out = new Map();
    for (const [id, rows] of Object.entries(res ?? {})) {
      out.set(id, rows.map((r) => ({
        startMs: _tsNorm(r.start), endMs: _tsNorm(r.end),
        change: r.change ?? null, state: r.state ?? null,
        sum: r.sum ?? null, mean: r.mean ?? null,
      })));
    }
    return out;
  });
}

// Hourly production (kWh per hour bucket) from a TOTAL_INCREASING sensor.
async function hourlyActuals(hass, producedEntityId, startISO, endISO) {
  const m = await wsStats(hass, {
    ids: [producedEntityId], startISO, endISO, period: "hour", types: ["change"],
  });
  return m.get(producedEntityId) ?? [];
}
// Daily production for the last n days (recorder handles local days + DST).
async function dailyActuals(hass, producedEntityId, nDays) {
  const start = new Date(localMidnightMs(hass, Date.now(), nDays)).toISOString();
  const m = await wsStats(hass, {
    ids: [producedEntityId], startISO: start, endISO: null,
    period: "day", types: ["change"],
  });
  return m.get(producedEntityId) ?? [];
}

// Day-ahead issued values, keyed by the evening they were issued.
//
// Two sources, in this order. Since integration 1.20.6 the numbers come from
// the integration itself: the day-ahead accuracy sensor carries a `history`
// block, day by day, plant and per string -- the very pairs that score is
// computed from. Before that they had to be rebuilt from recorder statistics of
// forecast_tomorrow — which worked only as a side effect of a `state_class`
// that a forecast should never have carried, and silently produced nothing
// for anyone whose recorder excluded the entity. That path stays for older
// installs and is where the second function below comes from.
async function issuedForecasts(hass, scope, nDays, issueHour) {
  const own = await issuedFromIntegration(hass, scope);
  if (own) { own.source = "integration"; return own; }
  const stats = await issuedFromStatistics(hass, scope.tomorrowId, nDays, issueHour);
  stats.source = "statistics";
  return stats;
}

// "2026-08-03" -> "2026-08-02". Component arithmetic through UTC, so a DST
// boundary cannot shift a calendar day.
function previousDayKey(dayKey) {
  const [y, m, d] = dayKey.split("-").map(Number);
  const t = Date.UTC(y, m - 1, d) - 86400000;
  const p = new Date(t);
  const pad = (n) => String(n).padStart(2, "0");
  return `${p.getUTCFullYear()}-${pad(p.getUTCMonth() + 1)}-${pad(p.getUTCDate())}`;
}

async function issuedFromIntegration(hass, scope) {
  // On the day-ahead accuracy sensor, not on deviation_yesterday: these are
  // the scored pairs, and deviation_yesterday sums every logged hour and the
  // whole measured day, which is a slightly different number for the same day.
  const srcId = await plantSibling(hass, scope.entityId, "wmape_day_ahead_30d");
  const history = srcId ? hass.states[srcId]?.attributes?.history : null;
  if (!history) return null;
  let series = history.plant;
  if (scope.stringName) {
    // The history is keyed by string_id, which survives a rename; the name
    // the registry model carries is mapped through strings_detail.
    const sdId = await plantSibling(hass, scope.entityId, "strings_detail");
    const sid = sdId
      ? hass.states[sdId]?.attributes?.strings?.[scope.stringName]?.string_id
      : null;
    series = sid ? history.strings?.[sid] : null;
  }
  if (!Array.isArray(series)) return null;
  const byIssueDay = new Map();
  for (const row of series) {
    const [dayKey, predicted] = row ?? [];
    // The integration keys a row by the day it was FOR; the caller asks by
    // the evening it was issued.
    if (dayKey && predicted != null) byIssueDay.set(previousDayKey(dayKey), predicted);
  }
  return byIssueDay;
}

// Fallback for integrations before 1.20.6: for each local day D, the
// forecast_tomorrow state as recorded in the hour bucket that STARTS at
// issue_hour on D-1 (the integration floor_hour()-stamps 18:xx runs to
// issued_at=18:00 and overwrites within the issue hour, so the last state in
// the 18->19 bucket IS the final day-ahead figure). Fallback: last bucket
// ending within (issue_hour, issue_hour+2]. Missing bucket => null ("no
// forecast issued").
async function issuedFromStatistics(hass, tomorrowEntityId, nDays, issueHour) {
  const start = new Date(localMidnightMs(hass, Date.now(), nDays + 1)).toISOString();
  const m = await wsStats(hass, {
    ids: [tomorrowEntityId], startISO: start, endISO: null,
    period: "hour", types: ["state"],
  });
  const rows = m.get(tomorrowEntityId) ?? [];
  const byIssueDay = new Map(); // dayKey of issue evening -> value
  for (const r of rows) {
    if (r.state == null) continue;
    const p = localParts(hass, r.startMs);
    if (p.hour === issueHour) byIssueDay.set(p.dayKey, r.state);
    else if (p.hour > issueHour && p.hour <= issueHour + 1 && !byIssueDay.has(p.dayKey)) {
      byIssueDay.set(p.dayKey, r.state); // fallback bucket
    }
  }
  return byIssueDay; // caller maps: soll(day D) = byIssueDay.get(dayKey(D-1))
}

// ---- history (PV Strings response services, optional) ----------------------

// Two read-only services answer "what did it look like back then": one day in
// hours and five-minute intervals, and every week since the start. They are
// detected by name, never by version — an integration without them gets
// exactly the dashboard it had, with no stepper anywhere.
function hasPvsService(hass, name) {
  return !!hass?.services?.pvstrings?.[name];
}
async function callPvsService(hass, service, data) {
  const res = await hass.callWS({
    type: "call_service", domain: "pvstrings", service,
    service_data: data, return_response: true,
  });
  return res?.response ?? null;
}
// A past day does not change any more, a week list only once a day — both
// cache for half an hour. Today is never fetched: it is the live card.
// A malformed answer throws, and cachedWS drops a rejected promise — one bad
// response must not blank every card of the plant for half an hour.
function historyDay(hass, entryId, dayKey) {
  return cachedWS(`hist-day|${entryId}|${dayKey}`, 30 * 60000, async () => {
    const r = await callPvsService(hass, "get_day", { config_entry_id: entryId, date: dayKey });
    if (!r || typeof r !== "object" || typeof r.plant !== "object" || r.plant === null) {
      throw new Error("get_day: unexpected response");
    }
    return r;
  });
}
function historyWeeks(hass, entryId) {
  return cachedWS(`hist-weeks|${entryId}`, 30 * 60000, async () => {
    const r = await callPvsService(hass, "get_weeks", { config_entry_id: entryId });
    if (!r || !Array.isArray(r.weeks)) throw new Error("get_weeks: unexpected response");
    return r;
  });
}

// The day being looked at, per plant, shared by every card of that plant: step
// the plant chart back and the string charts below it follow — nobody wants
// to click six cards to the same Tuesday. null is today. A wall tablet must
// not stay on last Tuesday for good, so it falls back to today after a quiet
// quarter of an hour.
const HIST_DAY_EVENT = "pvstrings-history-day";
const HIST_IDLE_MS = 15 * 60000;
const _histDay = new Map();
function historyDayOf(entryId) {
  return _histDay.get(entryId)?.dayKey ?? null;
}
function setHistoryDay(entryId, dayKey) {
  const prev = _histDay.get(entryId);
  if (prev?.timer) clearTimeout(prev.timer);
  if (dayKey) {
    const timer = setTimeout(() => setHistoryDay(entryId, null), HIST_IDLE_MS);
    _histDay.set(entryId, { dayKey, timer });
  } else {
    _histDay.delete(entryId);
  }
  window.dispatchEvent(new CustomEvent(HIST_DAY_EVENT, { detail: { entryId } }));
}

// "2026-08-03" -> "2026-08-04"; the counterpart of previousDayKey.
function nextDayKey(dayKey) {
  const [y, m, d] = dayKey.split("-").map(Number);
  const p = new Date(Date.UTC(y, m - 1, d) + 86400000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${p.getUTCFullYear()}-${pad(p.getUTCMonth() + 1)}-${pad(p.getUTCDate())}`;
}
// Start of an HA-local calendar day, as epoch ms. UTC noon of that date lies
// on that local day for every zone between -12 and +11; the loop covers the
// rest instead of assuming it.
function dayKeyStartMs(hass, dayKey) {
  const [y, m, d] = dayKey.split("-").map(Number);
  let ms = Date.UTC(y, m - 1, d, 12);
  for (let i = 0; i < 4; i++) {
    const k = localParts(hass, ms).dayKey;
    if (k === dayKey) break;
    ms += k < dayKey ? 6 * 3600000 : -6 * 3600000;
  }
  return localMidnightMs(hass, ms);
}

// ISO 8601 week number of a local day key ("KW 37"): Thursday decides the year.
function isoWeekOf(dayKey) {
  const [y, m, d] = dayKey.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  dt.setUTCDate(dt.getUTCDate() + 3 - ((dt.getUTCDay() + 6) % 7));
  const jan4 = new Date(Date.UTC(dt.getUTCFullYear(), 0, 4));
  return 1 + Math.round(((dt - jan4) / 86400000 - 3 + ((jan4.getUTCDay() + 6) % 7)) / 7);
}
// "KW 37 · 8.9.–14.9." for a week row of get_weeks (week_start = local Monday)
function weekLabel(hass, weekStart) {
  const noon = dayKeyStartMs(hass, weekStart) + 12 * 3600000;
  return `${t(hass, "week_short", { n: isoWeekOf(weekStart) })} · ${fmtDayShort(hass, noon)}–${fmtDayShort(hass, noon + 6 * 86400000)}`;
}

// The ‹ label › control. `prev`/`next` false disables that side; `past`
// marks it as looking away from the live value.
// `pick` ({ value, min, max } day keys) lays an invisible date input over the
// label: tapping the label is tapping the input, so Safari, iOS and Firefox
// open their calendar natively, and Chrome gets showPicker() on the click.
function stepperHTML(hass, label, { prev, next, past, pick = null }) {
  return `<span class="pvs-step${past ? " past" : ""}">
    <button type="button" data-step="-1" aria-label="${esc(t(hass, "hist_prev"))}"${prev ? "" : " disabled"}>‹</button>
    ${pick
      ? `<span class="lbl pick" title="${esc(t(hass, "hist_pick"))}">${esc(label)}<input type="date" data-pick
          aria-label="${esc(t(hass, "hist_pick"))}" value="${esc(pick.value)}"${pick.min ? ` min="${esc(pick.min)}"` : ""}${pick.max ? ` max="${esc(pick.max)}"` : ""}></span>`
      : `<span class="lbl">${esc(label)}</span>`}
    <button type="button" data-step="1" aria-label="${esc(t(hass, "hist_next"))}"${next ? "" : " disabled"}>›</button>
  </span>`;
}

/* ============================ SECTION: UI ================================ */

function problemHTML(hass, { reason, entity, missing = [], hint }) {
  const lines = missing.map((mi) =>
    `<div>${t(hass, "needs_attr", { attr: `<code>${mi.attr}</code>`, entity: `<code>${entity ?? "?"}</code>`, since: mi.since })}</div>`
  ).join("");
  return `<div class="pvs-problem">
    <span class="ico">⚠︎</span>
    <div>
      ${reason ? `<div>${reason}</div>` : ""}
      ${lines}${missing.length ? `<div>${t(hass, "found_without")}</div>` : ""}
      ${entity ? `<div class="ent">${entity}</div>` : ""}
      ${hint ? `<div class="ent">${hint}</div>` : ""}
    </div>
  </div>`;
}

function withheldHTML(text) {
  return `<span class="pvs-withheld">◌ ${text}</span>`;
}

// The "?" in a card head. `key` names a help_<key> string; the tooltip
// controller renders it (bold/italic only — help is prose, not a document).
function helpChip(key) {
  return `<span class="pvs-help" data-help="${key}" role="button" tabindex="0">?</span>`;
}
function miniMd(s) {
  return esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\*(.+?)\*/g, "<i>$1</i>");
}

// Translated label for a raw attribute key (collector counters, learn-cycle
// tallies). Falls back to the key with underscores spaced — a new key on a
// newer integration must still render, only less prettily.
function keyLabel(hass, key) {
  const s = STR[langOf(hass)]?.["lbl_" + key] ?? STR.en["lbl_" + key];
  return s ?? String(key).replaceAll("_", " ");
}

function esc(s) {
  // the apostrophe too: JSON payloads sit in single-quoted data attributes
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

class PvsBaseCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = null;
    this._renderToken = 0;
  }
  setConfig(config) {
    this._config = config ?? {};
    if (this._hass) this._render();
  }
  set hass(h) {
    const prev = this._hass;
    this._hass = h;
    if (!this._config) return;
    this.toggleAttribute("dark", isDarkTheme(h, this));
    if (this._shouldUpdate(prev, h)) this._render();
  }
  get hass() { return this._hass; }
  // Cards override: list of entity_ids whose state object identity gates re-render.
  watchedEntities() {
    return this._config?.entity ? [this._config.entity] : [];
  }
  _shouldUpdate(prev, next) {
    if (!prev) return true;
    for (const id of [...this.watchedEntities(), ...(this._extraWatched ?? [])]) {
      if (id && prev.states[id] !== next.states[id]) return true;
    }
    return false;
  }
  _fireMoreInfo(entityId) {
    // bubbles + composed: the event must escape the shadow root to reach HA.
    this.dispatchEvent(new CustomEvent("hass-more-info", {
      bubbles: true, composed: true, detail: { entityId },
    }));
  }
  // Shared: wire delegated click handling for [data-more-info] elements.
  _wireMoreInfo() {
    if (this._miWired) return;
    this._miWired = true;
    this.shadowRoot.addEventListener("click", (ev) => {
      const el = ev.target.closest?.("[data-more-info]");
      if (el) this._fireMoreInfo(el.getAttribute("data-more-info"));
    });
  }
  getCardSize() { return 4; }
  _render() { /* overridden */ }
}

// ---- tooltip controller (one per card) ------------------------------------

// Every card gets [data-help] handling for free: the "?" chip toggles an
// inline panel under the card head with its help_<key> text. Inline, not a
// floating tip: prose is read, not glanced at, it must work on touch, and a
// floating box would be clipped by the card on exactly the short cards
// (status strip, maturity) that need it most.
function wireTooltip(card, { selector, content } = {}) {
  const root = card.shadowRoot;
  const hide = () => root.querySelector(".pvs-tip")?.classList.remove("on");
  const show = (target, ev) => {
    const tip = root.querySelector(".pvs-tip");
    if (!tip) return;
    const html = content(target, ev);
    if (!html) { hide(); return; }
    tip.innerHTML = html;
    // measure at the left edge, so the width is the content's own and not
    // whatever the previous position left as room
    tip.style.left = "0px"; tip.style.top = "0px";
    tip.classList.add("on");
    const cardEl = root.querySelector("ha-card");
    const cr = cardEl.getBoundingClientRect();
    const tr = tip.getBoundingClientRect();
    let x = ev.clientX - cr.left + 12;
    let y = ev.clientY - cr.top + 12;
    if (x + tr.width > cr.width - 8) x = ev.clientX - cr.left - tr.width - 12;
    if (y + tr.height > cr.height - 8) y = cr.height - tr.height - 8;
    tip.style.left = `${Math.max(4, x)}px`;
    tip.style.top = `${Math.max(4, y)}px`;
  };
  if (selector) {
    root.addEventListener("pointermove", (ev) => {
      const el = ev.target.closest?.(selector);
      if (el) show(el, ev);
      else hide();
    });
    root.addEventListener("pointerleave", () => hide());
  }
  const toggleHelp = (h) => {
    const key = h.getAttribute("data-help");
    const open = root.querySelector(".pvs-helpbox");
    root.querySelectorAll(".pvs-help.on").forEach((e) => e.classList.remove("on"));
    if (open) { open.remove(); if (open.dataset.key === key) return; }
    const box = document.createElement("div");
    box.className = "pvs-helpbox"; box.dataset.key = key;
    box.innerHTML = miniMd(t(card._hass, "help_" + key));
    (h.closest(".pvs-head") ?? h.parentElement).insertAdjacentElement("afterend", box);
    h.classList.add("on");
  };
  root.addEventListener("click", (ev) => {
    const h = ev.target.closest?.("[data-help]");
    if (!h) return;
    ev.stopPropagation();
    toggleHelp(h);
  });
  root.addEventListener("keydown", (ev) => {
    const h = ev.target.closest?.("[data-help]");
    if (h && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); toggleHelp(h); }
  });
}

// ---- minimal visual editor -------------------------------------------------

function makeEditor(schema) {
  return class extends HTMLElement {
    setConfig(config) { this._config = config; this._draw(); }
    set hass(h) { this._h = h; this._draw(); }
    _draw() {
      if (!this._h || !this._config) return;
      if (customElements.get("ha-form")) {
        if (!this._form) {
          this._form = document.createElement("ha-form");
          this._form.addEventListener("value-changed", (ev) => {
            ev.stopPropagation();
            this.dispatchEvent(new CustomEvent("config-changed", {
              bubbles: true, composed: true,
              detail: { config: { ...this._config, ...ev.detail.value } },
            }));
          });
          this.appendChild(this._form);
        }
        this._form.hass = this._h;
        this._form.data = this._config;
        this._form.schema = schema;
        this._form.computeLabel = (s) => s.label ?? s.name;
      } else if (!this._fallback) {
        // Editors are deliberately dumb; YAML always works without them.
        this._fallback = true;
        this.innerHTML = `<p style="font-size:12px">ha-form unavailable — edit this card in YAML.</p>`;
      }
    }
  };
}

const ENTITY_SCHEMA = { name: "entity", selector: { entity: { filter: { integration: "pvstrings" } } } };

/* ========================= SECTION: CARD:SKYMAP ========================== */

// Approximate count of 10°x5° sky bins the sun crosses in a year at `lat`
// (elevation >= 3°). Used for "≈ P% of the year's sun path". Memoized.
const _annualBins = new Map();
function annualSkyCells(lat) {
  const key = Math.round(lat * 10);
  if (_annualBins.has(key)) return _annualBins.get(key);
  const rad = Math.PI / 180;
  const bins = new Set();
  for (let doy = 0; doy < 365; doy += 14) {
    const decl = 23.44 * Math.sin(2 * Math.PI * (284 + doy) / 365);
    for (let min = 0; min < 1440; min += 20) {
      const H = (min / 4 - 180) * rad; // hour angle, solar noon = 0
      const sinEl = Math.sin(lat * rad) * Math.sin(decl * rad)
        + Math.cos(lat * rad) * Math.cos(decl * rad) * Math.cos(H);
      const el = Math.asin(sinEl) / rad;
      if (el < 3) continue;
      let az = Math.acos(
        (Math.sin(decl * rad) - sinEl * Math.sin(lat * rad))
        / (Math.cos(Math.asin(sinEl)) * Math.cos(lat * rad))
      ) / rad;
      if (H > 0) az = 360 - az;
      bins.add(`${Math.floor(az / 10)}|${Math.floor(el / 5)}`);
    }
  }
  _annualBins.set(key, bins.size);
  return bins.size;
}

// Cut the azimuth domain at the largest circular gap between observed bins.
function azDomain(azValues) {
  const uniq = [...new Set(azValues)].sort((a, b) => a - b);
  if (!uniq.length) return { start: 0, span: 360 };
  if (uniq.length === 1) return { start: uniq[0] - 10, span: 30 };
  let gapAt = 0, gapSize = -1;
  for (let i = 0; i < uniq.length; i++) {
    const next = uniq[(i + 1) % uniq.length];
    const gap = ((next - uniq[i] - 10) + 360) % 360;
    if (gap > gapSize) { gapSize = gap; gapAt = next; }
  }
  const span = 360 - gapSize;
  return { start: gapAt, span };
}

const COMPASS = { 0: "N", 90: "E", 180: "S", 270: "W" };

class PvsSkyMapCard extends PvsBaseCard {
  setConfig(config) {
    if (!config?.entity) {
      this._config = config ?? {};
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}</style><ha-card>${problemHTML(this._hass, { reason: t(this._hass, "no_entity_config") })}</ha-card>`;
      return;
    }
    this._layer = "pooled";
    this._shadingId = undefined; // unresolved
    super.setConfig(config);
  }
  watchedEntities() {
    return [this._config?.entity, this._shadingId].filter(Boolean);
  }
  getCardSize() { return 5; }
  getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }

  async _resolveSiblings() {
    if (this._shadingId !== undefined || !this._hass) return;
    this._shadingId = null;
    try {
      this._shadingId = await sibling(this._hass, this._config.entity, "string_shading_now");
      this._extraWatched = [this._shadingId].filter(Boolean);
      if (this._shadingId) this._render();
    } catch (_) { /* registry unavailable: sun marker simply absent */ }
  }

  _render() {
    const hass = this._hass, cfg = this._config;
    if (!hass || !cfg?.entity) return;
    this._resolveSiblings();
    const st = hass.states[cfg.entity];
    const card = (inner) => {
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${SKY_CSS}</style><ha-card>${inner}<div class="pvs-tip"></div></ha-card>`;
      this._wire();
    };
    if (!st) return card(problemHTML(hass, { reason: t(hass, "entity_missing", { entity: cfg.entity }) }));
    const need = requireFeatures(st, ["sky_cells", "sky_ratio", "sky_level"]);
    if (!need.ok) return card(problemHTML(hass, { entity: cfg.entity, missing: need.missing }));

    const attrs = st.attributes;
    const cells = attrs.cells ?? [];
    const shading = this._shadingId ? hass.states[this._shadingId] : null;
    const title = cfg.title ?? shading?.attributes?.name
      ?? attrs.friendly_name?.replace(/ (Himmelskarte|Sky map)$/i, "") ?? cfg.entity;

    // ---- header ----
    // level: null is structural "nothing" (single string / too few shared
    // epochs), not "not yet" — method-only chip, never the withheld style.
    const level = attrs.level;
    const fitTxt = t(hass, attrs.fit_method === "differential"
      ? "sky_fit_differential" : "sky_fit_absolute");
    const levelBadge = level == null
      ? `<span class="pvs-chip clickable" data-more-info="${cfg.entity}"
           title="${esc(t(hass, "sky_level_none"))}">${fitTxt}</span>`
      : `<span class="pvs-chip clickable" data-more-info="${cfg.entity}"
           title="${esc(t(hass, "sky_level_tip"))}">
           ${t(hass, "sky_level")} <span class="v">${fmtNum(hass, level, 2)}</span>
           <span class="pvs-sub">· ${fitTxt}</span></span>`;
    const pooledCount = cells.filter((c) => c.season == null).length;
    const lat = hass.config?.latitude;
    const annual = lat != null ? annualSkyCells(lat) : null;
    const share = annual ? Math.round((pooledCount / annual) * 100) : null;
    const cellChip = `<span class="pvs-chip clickable" data-more-info="${cfg.entity}">
        <span class="v">${pooledCount}</span> ${t(hass, "sky_cells", { n: "" }).replace("{n} ", "").trim() || "cells"}
        ${share != null ? `<span class="pvs-sub">· ${t(hass, "sky_share_of_year", { pct: share })}</span>` : ""}
      </span>`;
    // ---- empty-but-valid state (rule 1: "not yet", never a blank) ----
    if (!cells.length) {
      const obs = shading?.attributes?.observations ?? "?";
      return card(`
        <div class="pvs-head"><span class="pvs-title">${esc(title)}</span>${levelBadge}</div>
        ${withheldHTML(t(hass, "sky_no_cells", { obs }))}`);
    }

    // ---- layers, dedupe pooled vs seasonal ----
    const layers = { pooled: new Map(), ascending: new Map(), descending: new Map() };
    for (const c of cells) {
      const k = `${c.az}|${c.el}`;
      layers[c.season == null ? "pooled" : c.season]?.set(k, c);
    }
    const hasSeasons = (cfg.seasons ?? true) &&
      (layers.ascending.size + layers.descending.size > 0);
    if (!hasSeasons) this._layer = "pooled";
    const layer = layers[this._layer] ?? layers.pooled;

    // ---- window over ALL layers so the frame doesn't jump when toggling ----
    const allCells = cells;
    const { start, span } = azDomain(allCells.map((c) => c.az));
    const els = allCells.map((c) => c.el);
    const elMin = Math.max(0, Math.min(...els) - 5);
    const elMax = Math.min(85, Math.max(...els) + 5);
    const nAz = Math.round(span / 10) + 2; // +1 bin margin each side
    const nEl = Math.round((elMax - elMin) / 5) + 1;
    const CW = 26, CH = 17, PAD_L = 34, PAD_B = 22, PAD_T = 6, PAD_R = 6;
    const W = PAD_L + nAz * CW + PAD_R, H = PAD_T + nEl * CH + PAD_B;
    // Continuous mapping: column 0's lower az edge is start-10; the top of
    // the highest band (lower edge elMax) is elMax+5.
    const xOf = (az) => PAD_L + ((((az - start + 10) % 360) + 360) % 360) / 10 * CW;
    const yOf = (el) => PAD_T + (elMax + 5 - el) / 5 * CH;

    // ---- cells ----
    let rects = "";
    for (let ai = 0; ai < nAz; ai++) {
      const az = ((start - 10 + ai * 10) % 360 + 360) % 360;
      for (let ei = 0; ei < nEl; ei++) {
        const el = elMax - ei * 5;
        if (el < 0) continue;
        const c = layer.get(`${az}|${el}`);
        const x = PAD_L + ai * CW, y = PAD_T + ei * CH;
        if (c) {
          // observed cells are filled AND outlined — the light end of the
          // shadow ramp must never melt into the card background (a clear
          // cell is not "nothing")
          rects += `<rect class="cell" x="${x}" y="${y}" width="${CW}" height="${CH}"
            fill="${lossColor(c.loss)}" stroke="var(--pvs-cell-stroke)" stroke-width="1"
            data-cell='${esc(JSON.stringify({ az, el, loss: c.loss, ratio: c.ratio, n: c.n, season: c.season }))}'/>`;
        } else {
          // never observed: unfilled + hatched — on the shadow ramp neither
          // end may collide with this
          rects += `<rect x="${x}" y="${y}" width="${CW}" height="${CH}" fill="url(#pvs-hatch)"
            stroke="none" data-unobs="1"/>`;
        }
      }
    }

    // ---- axes ----
    let axes = "";
    for (let az = 0; az < 360; az += 30) {
      const rel = (((az - start + 10) % 360) + 360) % 360;
      if (rel > span + 20) continue;
      const x = PAD_L + rel / 10 * CW;
      axes += `<text x="${x}" y="${H - 8}" text-anchor="middle" class="axis">${COMPASS[az] ?? az + "°"}</text>`;
    }
    for (let el = Math.ceil(elMin / 15) * 15; el <= elMax; el += 15) {
      axes += `<text x="${PAD_L - 6}" y="${yOf(el) + 4}" text-anchor="end" class="axis">${el}°</text>`;
    }

    // ---- sun marker ----
    let sun = "", sunLine = "";
    if ((cfg.show_sun ?? true) && shading) {
      const sa = shading.attributes;
      if (sa.sun_elevation != null && sa.sun_elevation >= 3) {
        // Signed offset from the window's left edge: xOf wraps modulo 360, so
        // a morning sun just left of the window came out at +340 and was
        // pinned to the west edge. Split the unseen arc at its middle instead.
        const extent = nAz * 10;
        let rel = (((sa.sun_azimuth - start + 10) % 360) + 360) % 360;
        if (rel > extent + (360 - extent) / 2) rel -= 360;
        const sx = PAD_L + rel / 10 * CW;
        let sy = yOf(sa.sun_elevation);
        const clampedX = Math.max(PAD_L + 6, Math.min(W - PAD_R - 6, sx));
        const outside = clampedX !== sx || sy < PAD_T || sy > H - PAD_B;
        sy = Math.max(PAD_T + 6, Math.min(H - PAD_B - 6, sy));
        sun = `<g class="sun" data-more-info="${this._shadingId}">
          <circle cx="${clampedX}" cy="${sy}" r="7" fill="var(--pvs-sun)"
            stroke="var(--card-background-color)" stroke-width="2"/>
          <circle cx="${clampedX}" cy="${sy}" r="10.5" fill="none"
            stroke="var(--pvs-sun)" stroke-width="1" opacity="0.5" class="sun-ring"/>
        </g>`;
        // Below the map, not inside it: a young map is only a few cells wide
        // and any label in the SVG is cut off at its edge.
        if (outside) sunLine = `<div class="sky-warnline dim">☀ ${esc(t(hass, "sky_sun_outside"))}</div>`;
      } else {
        sunLine = `<div class="sky-warnline dim">☾ ${t(hass, "sky_sun_below")}</div>`;
      }
    }

    // ---- layer switch ----
    const seg = hasSeasons ? `<div class="sky-seg">
        ${["pooled", "ascending", "descending"].map((l) => `
          <button class="${this._layer === l ? "on" : ""}" data-layer="${l}">
            ${t(hass, "sky_layer_" + l)}</button>`).join("")}
      </div>` : "";

    // ---- legend: ramp scale + unobserved swatch ----
    const rampStops = Array.from({ length: 8 }, (_, i) =>
      `<stop offset="${(i / 7) * 100}%" stop-color="var(--pvs-loss-${i})"/>`).join("");
    const legend = `<div class="pvs-legend">
      <span class="it"><svg width="72" height="10"><defs>
        <linearGradient id="pvs-ramp">${rampStops}</linearGradient></defs>
        <rect width="72" height="10" rx="2" fill="url(#pvs-ramp)"/></svg>
        ${t(hass, attrs.fit_method === "differential" ? "sky_loss_clear" : "sky_loss")} 0–95%</span>
      <span class="it"><svg width="14" height="12">
        <rect x="0.5" y="0.5" width="13" height="11" rx="2" fill="url(#pvs-hatch-l)"
          stroke="var(--pvs-cell-stroke)" stroke-width="1"/></svg>
        ${t(hass, "sky_unobserved")}</span>
    </div>`;

    card(`
      <div class="pvs-head">
        <span class="pvs-title">${esc(title)}</span>
        ${seg}${levelBadge}${cellChip}
      </div>
      ${sunLine}
      <div class="sky-wrap">
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
          <defs>${hatchPattern("pvs-hatch")}${hatchPattern("pvs-hatch-l")}</defs>
          ${rects}${axes}${sun}
        </svg>
      </div>
      ${legend}`);
  }

  _wire() {
    this._wireMoreInfo();
    if (this._wired) return;
    this._wired = true;
    this.shadowRoot.addEventListener("click", (ev) => {
      const b = ev.target.closest?.("[data-layer]");
      if (b) { this._layer = b.getAttribute("data-layer"); this._render(); }
    });
    wireTooltip(this, {
      selector: "[data-cell],[data-unobs]",
      content: (el) => {
        const hass = this._hass;
        if (el.hasAttribute("data-unobs")) {
          return `<div class="h">${t(hass, "sky_unobserved")}</div>`;
        }
        const c = JSON.parse(el.getAttribute("data-cell"));
        const seasonTxt = c.season == null ? t(hass, "sky_pooled")
          : t(hass, "sky_season_" + c.season);
        const differential = hass?.states[this._config?.entity]
          ?.attributes?.fit_method === "differential";
        return `<div class="h">${c.az}°–${c.az + 10}° · ${c.el}°–${c.el + 5}°</div>
          <div class="r"><span class="k">${t(hass, differential ? "sky_loss_clear" : "sky_loss")}</span><span class="v">${fmtNum(hass, c.loss, 1)} %</span></div>
          <div class="r"><span class="k">ratio</span><span class="v">${fmtNum(hass, c.ratio, 3)}${c.ratio > 1 ? " ↑" : ""}</span></div>
          <div class="r"><span class="k">n</span><span class="v">${fmtNum(hass, c.n, 1)}</span></div>
          <div class="pvs-sub">${seasonTxt}${c.ratio > 1 ? `<br>${t(hass, "sky_ratio_gt1")}` : ""}</div>`;
      },
    });
  }

  static getConfigElement() {
    return document.createElement("pvstrings-sky-map-editor");
  }
  static getStubConfig(hass, entities) {
    const guess = (entities ?? []).find((e) =>
      hass.states[e]?.attributes?.cells !== undefined && e.startsWith("sensor."));
    return { entity: guess ?? "" };
  }
}

const SKY_CSS = `
  .sky-wrap svg { width: 100%; height: auto; display: block; }
  .sky-warnline {
    font-size: 11.5px; line-height: 1.5; margin: 2px 0 8px;
    color: var(--primary-text-color);
    padding: 6px 9px; border-radius: 6px;
    background: color-mix(in srgb, var(--warning-color, #ffa600) 10%, transparent);
  }
  .sky-warnline.dim { background: var(--pvs-chip-bg); color: var(--secondary-text-color); }
  .sky-seg { display: inline-flex; border: 1px solid var(--pvs-hairline); border-radius: 6px; overflow: hidden; }
  .sky-seg button {
    all: unset; font-size: 10.5px; padding: 4px 8px; cursor: pointer;
    color: var(--secondary-text-color); transition: background 150ms;
  }
  .sky-seg button:hover { background: var(--pvs-chip-bg); }
  .sky-seg button.on { background: var(--pvs-chip-bg); color: var(--primary-text-color); font-weight: 600; }
  .cell { transition: opacity 150ms; }
  .cell:hover { opacity: 0.75; }
  .sun { cursor: pointer; }
  .sun-ring { animation: pvs-pulse 3s ease-in-out infinite; transform-origin: center; }
  @keyframes pvs-pulse { 0%,100% { opacity: 0.5; } 50% { opacity: 0.15; } }
`;

/* ======================== SECTION: CARD:FORECAST ========================= */

function niceMax(v) {
  if (v <= 0) return 1;
  const mag = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10]) {
    if (v <= m * mag) return m * mag;
  }
  return 10 * mag;
}

class PvsForecastCard extends PvsBaseCard {
  setConfig(config) {
    this._resolved = null;
    this._actuals = null;
    this._actualsProblem = null;
    this._past = null;
    this._power = null;
    this._earliest = null;
    this._earliestAsked = false;
    super.setConfig(config ?? {});
  }
  // Every forecast card of a plant follows the day that plant is looked at.
  connectedCallback() {
    if (this._histListener) return;
    this._histListener = (ev) => {
      if (!ev.detail?.entryId || ev.detail.entryId !== this._resolved?.entryId) return;
      // a failed day is tried again once somebody steps — failures are not cached
      if (this._past?.problem) this._past = null;
      this._render();
    };
    window.addEventListener(HIST_DAY_EVENT, this._histListener);
    if (this._hass && this._config) this._render();
  }
  disconnectedCallback() {
    window.removeEventListener(HIST_DAY_EVENT, this._histListener);
    this._histListener = null;
  }
  watchedEntities() {
    const r = this._resolved;
    return [this._config?.entity, r?.tomorrowId, r?.dayAfterId,
      r?.producedIds?.[0]].filter(Boolean);
  }
  getCardSize() { return 5; }
  // "full": a section with column_span > 1 doubles its internal grid, and a
  // card pinned to 12 columns would only fill half of it.
  getGridOptions() { return { columns: "full", rows: "auto" }; }

  async _resolve() {
    if (this._resolved !== null || !this._hass || !this._config?.entity) return;
    this._resolved = false; // in flight
    const hass = this._hass, id = this._config.entity;
    try {
      const m = await getRegistryModel(hass);
      const info = m.byEntityId.get(id);
      const kind = info?.level ?? "plant";
      const r = { kind };
      // the plant's config entry and, for a string, its string_id — the
      // handles the history service answers by
      r.entryId = (kind === "plant" ? info?.node?.entryId : info?.node?.plant?.entryId)
        ?? info?.node?.entryId ?? null;
      r.stringId = kind === "string" ? info.node.scopeId ?? null : null;
      if (kind === "plant") {
        r.tomorrowId = info.node.byKey.forecast_tomorrow ?? null;
        r.dayAfterId = info.node.byKey.forecast_day_after ?? null;
        r.remainingId = info.node.byKey.forecast_remaining ?? null;
        r.producedIds = [info.node.byKey.produced_today].filter(Boolean);
        r.powerId = info.node.byKey.power_now ?? null;
      } else if (kind === "string") {
        r.remainingId = info.node.byKey.string_forecast_remaining ?? null;
        r.tomorrowId = info.node.byKey.string_forecast_tomorrow ?? null;
        r.producedIds = [info.node.byKey.string_produced_today].filter(Boolean);
        // the string's configured power entity, via the plant's strings_detail
        const sdId = info.node.plant?.byKey?.strings_detail;
        const sd = sdId ? hass.states[sdId]?.attributes?.strings : null;
        r.powerId = sd?.[info.node.name]?.power_entity ?? null;
      } else { // group: match member names against string devices
        const names = hass.states[id]?.attributes?.strings ?? [];
        const ids = names.map((n) =>
          m.strings.find((s) => s.name === n)?.byKey?.string_produced_today ?? null);
        r.producedIds = ids.filter(Boolean);
        r.membersUnresolved = ids.some((x) => !x);
      }
      this._resolved = r;
      this._render();
      this._loadActuals();
      if ((this._config.style ?? "bars") === "line") this._loadPower();
    } catch (e) {
      this._resolved = { kind: "plant", producedIds: [], error: String(e) };
      this._render();
    }
  }

  async _loadActuals() {
    const hass = this._hass, r = this._resolved;
    if (!r || !(this._config.show_actual ?? true)) return;
    if (!r.producedIds?.length || r.membersUnresolved) {
      this._actualsProblem = r?.membersUnresolved ? "fc_actual_unresolved" : null;
      return;
    }
    const token = ++this._renderToken;
    const startMs = localMidnightMs(hass, Date.now());
    try {
      const stats = await wsStats(hass, {
        ids: r.producedIds,
        startISO: new Date(startMs).toISOString(),
        endISO: null, period: "hour", types: ["change"],
      });
      if (token !== this._renderToken) return;
      const byHour = new Map();
      let sawRows = false;
      for (const id of r.producedIds) {
        for (const row of stats.get(id) ?? []) {
          sawRows = true;
          if (row.change != null) {
            byHour.set(row.startMs, (byHour.get(row.startMs) ?? 0) + row.change);
          }
        }
      }
      this._actuals = byHour;
      this._actualsProblem = sawRows ? null : "stats_unavailable";
      this._render();
    } catch (_) {
      if (token === this._renderToken) {
        this._actualsProblem = "stats_unavailable";
        this._render();
      }
    }
  }

  // 5-minute mean power for the line style. Falls back to hourly statistics
  // (marked as such) when the power entity has no short-term statistics.
  async _loadPower() {
    const hass = this._hass, r = this._resolved;
    if (!r?.powerId) { this._power = null; return; }
    const token = this._renderToken; // don't outrace _loadActuals' bump
    const startMs = localMidnightMs(hass, Date.now());
    const stale = () => this._resolved !== r;
    try {
      const stats = await wsStats(hass, {
        ids: [r.powerId],
        startISO: new Date(startMs).toISOString(), endISO: null,
        period: "5minute", types: ["mean"], // units left as-is; W assumed below
      });
      if (stale()) return;
      const unit = hass.states[r.powerId]?.attributes?.unit_of_measurement ?? "W";
      const scale = unit === "kW" ? 1000 : 1;
      this._power = (stats.get(r.powerId) ?? [])
        .filter((x) => x.mean != null)
        .map((x) => ({ ms: x.startMs + 150000, w: x.mean * scale }));
      this._render();
    } catch (_) {
      if (stale()) return;
      this._power = null;
      this._render();
    }
  }

  // ---- the day being looked at ---------------------------------------------

  _historyOn() {
    const r = this._resolved;
    return this._config?.history !== false && !!r && r.kind !== "group"
      && !!r.entryId && hasPvsService(this._hass, "get_day");
  }
  // null is today — the live card; otherwise a past local day key
  _viewDay() {
    if (!this._historyOn()) return null;
    const d = historyDayOf(this._resolved.entryId);
    return d && d < localParts(this._hass, Date.now()).dayKey ? d : null;
  }
  _stepHTML(day) {
    if (!this._historyOn()) return "";
    const hass = this._hass;
    const cur = day ?? localParts(hass, Date.now()).dayKey;
    const noon = dayKeyStartMs(hass, cur) + 12 * 3600000;
    const label = day ? `${fmtWeekday(hass, noon)} ${fmtDayShort(hass, noon)}` : t(hass, "today");
    const earliest = this._earliest ?? null;
    // the calendar greys out days before the data; learn where that is before
    // anyone clicks (the answer for yesterday is cached and is the first step
    // back anyway)
    if (!earliest && !this._earliestAsked) {
      this._earliestAsked = true;
      historyDay(hass, this._resolved.entryId, previousDayKey(localParts(hass, Date.now()).dayKey))
        .then((d) => {
          if (d?.earliest_date && !this._earliest) { this._earliest = d.earliest_date; this._render(); }
        })
        // no retry: a render comes with every sensor tick, and the calendar
        // simply opens without a lower bound
        .catch(() => {});
    }
    const today = localParts(hass, Date.now()).dayKey;
    return stepperHTML(hass, label, {
      prev: !earliest || cur > earliest, next: !!day, past: !!day,
      pick: { value: cur, min: earliest, max: today },
    });
  }
  _pick(value) {
    const hass = this._hass, r = this._resolved;
    if (!r?.entryId || !/^\d{4}-\d{2}-\d{2}$/.test(value ?? "")) return;
    const today = localParts(hass, Date.now()).dayKey;
    setHistoryDay(r.entryId, value >= today ? null : value);
  }
  _step(dir) {
    const hass = this._hass, r = this._resolved;
    if (!r?.entryId) return;
    const today = localParts(hass, Date.now()).dayKey;
    const cur = this._viewDay() ?? today;
    const to = dir < 0 ? previousDayKey(cur) : nextDayKey(cur);
    setHistoryDay(r.entryId, to >= today ? null : to);
  }
  async _loadPast(day) {
    const r = this._resolved;
    if (this._past?.dayKey === day && this._past.resolved === r) return;
    // the request is identified by the resolution it was made for: a card
    // re-pointed at another plant, or stepped on, while this was in flight
    // has moved on, and the late answer is dropped
    const req = { dayKey: day, resolved: r };
    this._past = req;
    let next;
    try {
      next = { ...req, data: await historyDay(this._hass, r.entryId, day) };
    } catch (e) {
      next = { ...req, problem: e?.message ?? String(e) };
    }
    if (this._past !== req) return;
    if (next.data?.earliest_date) this._earliest = next.data.earliest_date;
    this._past = next;
    this._render();
  }

  // Everything a drawing needs, from the live sensors or from a past day. The
  // two views draw from this and nothing else, so a past Tuesday and today
  // cannot drift into looking different for reasons that are not the data.
  _liveSource(line) {
    const hass = this._hass, cfg = this._config;
    const days = cfg.days ?? (line ? 1 : 2);
    return {
      past: false, days, startMs: localMidnightMs(hass, Date.now()),
      windowMs: days * 24 * 3600000, nowMs: Date.now(),
      rows: this._rows(), actuals: this._actuals, power: this._power ?? null,
      dayAhead: null, totals: null,
    };
  }
  _pastSource(r, day) {
    const hass = this._hass, d = this._past.data;
    const series = r.stringId ? d.strings?.[r.stringId] : d.plant;
    const startMs = dayKeyStartMs(hass, day);
    const endMs = dayKeyStartMs(hass, nextDayKey(day));
    const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
    const hours = (series?.hours ?? [])
      .map((h) => ({
        ms: Date.parse(h.start), f: num(h.forecast_kwh), u: num(h.unshaded_kwh),
        da: num(h.day_ahead_kwh), daAt: h.day_ahead_issued_at ?? null,
        a: num(h.actual_kwh), cens: !!h.censored,
      }))
      .filter((x) => Number.isFinite(x.ms) && x.ms >= startMs && x.ms < endMs)
      .sort((x, y) => x.ms - y.ms);
    const rows = hours.filter((x) => x.f != null)
      .map((x) => ({ ms: x.ms, potential: x.f, unshaded: x.u }));
    const actuals = new Map(hours.filter((x) => x.a != null).map((x) => [x.ms, x.a]));
    const dayAhead = new Map(hours.filter((x) => x.da != null)
      .map((x) => [x.ms, { v: x.da, at: x.daAt }]));
    // five-minute power: a dense array from the day's start, null = no interval
    const iv = series?.intervals;
    const power = [];
    const ivStart = iv?.start ? Date.parse(iv.start) : NaN;
    if (Number.isFinite(ivStart) && Array.isArray(iv.power_w)) {
      const step = (iv.step_minutes ?? 5) * 60000;
      iv.power_w.forEach((w, i) => {
        if (typeof w === "number" && Number.isFinite(w)) power.push({ ms: ivStart + i * step + step / 2, w });
      });
    }
    const sum = (vals) => (vals.length ? vals.reduce((s, v) => s + v, 0) : null);
    const intervalsGone = !power.length && d.intervals_since && day < d.intervals_since && actuals.size
      ? t(hass, "hist_no_intervals", { date: fmtDayShort(hass, dayKeyStartMs(hass, d.intervals_since) + 12 * 3600000) })
      : null;
    return {
      past: true, day, days: 1, startMs, windowMs: endMs - startMs, nowMs: endMs,
      rows, actuals, power: power.length ? power : null, dayAhead,
      totals: {
        forecast: sum(rows.map((x) => x.potential)),
        actual: sum([...actuals.values()]),
        dayAhead: sum([...dayAhead.values()].map((x) => x.v)),
      },
      dayAheadGone: !dayAhead.size && d.day_ahead_since && day < d.day_ahead_since,
      intervalsGone,
    };
  }

  _renderLine(card, st, r, src) {
    const hass = this._hass, cfg = this._config;
    const title = cfg.title ?? st.attributes.friendly_name?.replace(/ (Prognose heute|Forecast today)$/i, "") ?? cfg.entity;
    const step = this._stepHTML(src.past ? src.day : null);
    const rows = src.rows;
    if (!rows.length && !(src.past && src.actuals.size)) {
      return card(`<div class="pvs-head"><span class="pvs-title">${esc(title)}</span>${step}</div>
        ${withheldHTML(t(hass, src.past ? "hist_no_data" : "fc_no_hours"))}`);
    }
    const isGroup = r.kind === "group";
    const showUnshaded = (cfg.show_unshaded ?? true) && !isGroup;
    const { startMs, windowMs, nowMs } = src;
    const hoursInWindow = Math.round(windowMs / 3600000);

    // series in W: forecast/unshaded at hour centers; actual 5-min or hourly
    const fcPts = rows.map((x) => ({ ms: x.ms + 1800000, v: x.potential * 1000 }));
    const unPts = showUnshaded
      ? rows.filter((x) => x.unshaded != null).map((x) => ({ ms: x.ms + 1800000, v: x.unshaded * 1000 })) : [];
    let actPts = [], hourlyFallback = false;
    if (src.power?.length) {
      actPts = src.power.filter((p) => p.ms >= startMs && p.ms <= nowMs)
        .map((p) => ({ ms: p.ms, v: Math.max(0, p.w) }));
    } else if (src.actuals) {
      hourlyFallback = true;
      actPts = [...src.actuals.entries()]
        .filter(([ms]) => ms + 3600000 <= nowMs + 60000)
        .sort((a, b) => a[0] - b[0])
        .map(([ms, kwh]) => ({ ms: ms + 1800000, v: kwh * 1000 }));
    }

    let maxV = 100;
    for (const p of [...fcPts, ...unPts, ...actPts]) maxV = Math.max(maxV, p.v);
    const yMax = niceMax(maxV * 1.06);
    // wide: for full-width placement — wider AND taller drawing area, so a
    // card spanning two columns reads as the hero chart, not as a squished
    // copy of the narrow one.
    const wide = !!cfg.wide;
    const PAD_L = 42, PAD_R = 6, PAD_T = 10, PAD_B = 22;
    const PH = wide ? 300 : 168;
    const PW = wide ? 1330 : 620;
    const W = PAD_L + PW + PAD_R, H = PAD_T + PH + PAD_B;
    const xOf = (ms) => PAD_L + ((ms - startMs) / windowMs) * PW;
    const yOf = (v) => PAD_T + PH - (v / yMax) * PH;

    const path = (pts, maxGap) => {
      let d = "", prev = null;
      for (const p of pts) {
        d += `${!prev || p.ms - prev.ms > maxGap ? "M" : "L"}${xOf(p.ms).toFixed(1)} ${yOf(p.v).toFixed(1)}`;
        prev = p;
      }
      return d;
    };
    // Monotone cubic interpolation (Fritsch–Carlson): passes through every
    // data point and never overshoots — it rounds the corners the smooth
    // physics implies without inventing extrema the data does not have.
    // (A plain Catmull-Rom/Bézier spline stays taboo per SPEC §2.)
    const smoothPath = (pts, maxGap) => {
      let d = "", run = [];
      const emit = () => {
        const P = run.map((p) => [xOf(p.ms), yOf(p.v)]);
        run = [];
        if (!P.length) return;
        d += `M${P[0][0].toFixed(1)} ${P[0][1].toFixed(1)}`;
        const n = P.length;
        if (n < 2) return;
        const h = [], delta = [];
        for (let i = 0; i < n - 1; i++) {
          h.push(P[i + 1][0] - P[i][0]);
          delta.push((P[i + 1][1] - P[i][1]) / h[i]);
        }
        const m = [delta[0]];
        for (let i = 1; i < n - 1; i++) {
          m.push(delta[i - 1] * delta[i] <= 0 ? 0 : (delta[i - 1] + delta[i]) / 2);
        }
        m.push(delta[n - 2]);
        for (let i = 0; i < n - 1; i++) {
          if (delta[i] === 0) { m[i] = 0; m[i + 1] = 0; continue; }
          if (m[i] / delta[i] > 3) m[i] = 3 * delta[i];
          if (m[i + 1] / delta[i] > 3) m[i + 1] = 3 * delta[i];
        }
        for (let i = 0; i < n - 1; i++) {
          const c1x = P[i][0] + h[i] / 3, c1y = P[i][1] + m[i] * h[i] / 3;
          const c2x = P[i + 1][0] - h[i] / 3, c2y = P[i + 1][1] - m[i + 1] * h[i] / 3;
          d += `C${c1x.toFixed(1)} ${c1y.toFixed(1)} ${c2x.toFixed(1)} ${c2y.toFixed(1)} ${P[i + 1][0].toFixed(1)} ${P[i + 1][1].toFixed(1)}`;
        }
      };
      for (const p of pts) {
        if (run.length && p.ms - run[run.length - 1].ms > maxGap) emit();
        run.push(p);
      }
      emit();
      return d;
    };
    // Forecast/unshaded are smoothed (hourly means of a smooth process);
    // the measured 5-minute line stays raw — its jitter is information.
    const fcLine = (cfg.smooth ?? true) ? smoothPath : path;
    // area under the actual line, one closed path per contiguous run
    const area = (pts, maxGap) => {
      let d = "", run = [];
      const flush = () => {
        if (run.length < 2) { run = []; return; }
        d += `M${xOf(run[0].ms).toFixed(1)} ${yOf(0).toFixed(1)}`
          + run.map((p) => `L${xOf(p.ms).toFixed(1)} ${yOf(p.v).toFixed(1)}`).join("")
          + `L${xOf(run[run.length - 1].ms).toFixed(1)} ${yOf(0).toFixed(1)}Z`;
        run = [];
      };
      for (const p of pts) {
        if (run.length && p.ms - run[run.length - 1].ms > maxGap) flush();
        run.push(p);
      }
      flush();
      return d;
    };
    const HOUR_GAP = 2 * 3600000, FIVE_GAP = 16 * 60000;
    const actGap = hourlyFallback ? HOUR_GAP : FIVE_GAP;

    // axes: y 0/half/max, x every 4h (1 day) or 6h
    let grid = "";
    const unitKw = yMax >= 10000;
    const yTicks = wide ? [0, 0.25, 0.5, 0.75, 1].map((f) => f * yMax) : [0, yMax / 2, yMax];
    for (const v of yTicks) {
      grid += `<line class="grid" x1="${PAD_L}" y1="${yOf(v)}" x2="${W - PAD_R}" y2="${yOf(v)}"/>
        <text class="axis" x="${PAD_L - 5}" y="${yOf(v) + 3}" text-anchor="end">${unitKw ? fmtNum(hass, v / 1000, 1) : fmtNum(hass, v, 0)}</text>`;
    }
    grid += `<text class="axis" x="${PAD_L - 5}" y="${PAD_T - 2}" text-anchor="end" style="font-size:8.5px">${unitKw ? "kW" : "W"}</text>`;
    const days = src.days;
    const stepH = wide ? (days === 1 ? 2 : 4) : (days === 1 ? 4 : 6);
    for (let hOff = 0; hOff <= hoursInWindow; hOff += stepH) {
      const ms = startMs + hOff * 3600000;
      const x = xOf(ms);
      if (hOff % 24 === 0 && hOff > 0 && hOff < hoursInWindow) {
        grid += `<line class="grid" x1="${x}" y1="${PAD_T}" x2="${x}" y2="${PAD_T + PH}"/>`;
      }
      if (hOff < hoursInWindow || days === 1) {
        grid += `<text class="axis" x="${x}" y="${H - 8}" text-anchor="middle">${fmtHour(hass, ms)}</text>`;
      }
    }
    // now marker
    let now = "";
    if (nowMs > startMs && nowMs < startMs + windowMs) {
      const nx = xOf(nowMs);
      now = `<line x1="${nx}" y1="${PAD_T - 3}" x2="${nx}" y2="${PAD_T + PH}"
        stroke="var(--pvs-measure)" stroke-width="1" stroke-dasharray="2 3" opacity="0.7"/>`;
    }

    // hero numbers: the live sensors today, the day's own sums for a past day
    const heroItem = (v, color, label, entity, right) => v == null || isNaN(v) ? "" :
      `<div class="fc-hero-item${right ? " right" : ""}${entity ? " clickable" : ""}"${entity ? ` data-more-info="${entity}"` : ""}>
        <span class="hv" style="color:var(${color})">${fmtNum(hass, v, 1)}<span class="hu">kWh</span></span>
        <span class="hl">${label}</span></div>`;
    let heroIst, heroProg;
    if (src.past) {
      heroIst = heroItem(src.totals.actual, "--pvs-measure", t(hass, "hist_hero_actual"), null, false);
      heroProg = heroItem(src.totals.forecast, "--pvs-model", t(hass, "hist_hero_forecast"), null, true);
    } else {
      const prodId = r.producedIds?.[0];
      const prodSt = prodId ? hass.states[prodId] : null;
      heroIst = heroItem(prodSt ? parseFloat(prodSt.state) : null, "--pvs-measure", t(hass, "fc_hero_ist"), prodId, false);
      heroProg = heroItem(isGroup ? st.attributes.today_kwh : parseFloat(st.state), "--pvs-model", t(hass, "fc_hero_prog"), cfg.entity, true);
    }

    this._lineGeom = { startMs, windowMs, PAD_L, PW, W };
    this._lineSeries = {
      fc: new Map(rows.map((x) => [x.ms, x])),
      act: actPts, hourlyFallback, dayAhead: src.dayAhead,
    };

    const notes = [];
    if (src.intervalsGone) notes.push(`<div class="fc-note">${src.intervalsGone}</div>`);
    else if (hourlyFallback && actPts.length) notes.push(`<div class="fc-note">${t(hass, "fc_hourly_fallback")}</div>`);
    if (!src.past && this._actualsProblem === "stats_unavailable" && !actPts.length) {
      notes.push(`<div class="fc-note">${t(hass, "stats_unavailable", { entity: r.powerId ?? r.producedIds?.join(", ") ?? "?" })}</div>`);
    }

    card(`
      <div class="fc-hero${wide ? " wide" : ""}">${heroIst}<div class="fc-hero-mid"><span class="fc-hero-title">${esc(title)}</span>${step}</div>${heroProg}</div>
      ${notes.join("")}
      <div class="fc-wrap"><svg class="fc-line" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="aspect-ratio:${W}/${H}">
        ${grid}
        ${actPts.length ? `<path d="${area(actPts, actGap)}" fill="var(--pvs-measure)" opacity="0.13"/>` : ""}
        ${unPts.length ? `<path d="${fcLine(unPts, HOUR_GAP)}" fill="none" stroke="var(--pvs-model-ghost)" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>` : ""}
        ${fcPts.length ? `<path d="${fcLine(fcPts, HOUR_GAP)}" fill="none" stroke="var(--pvs-model)" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>` : ""}
        ${actPts.length ? `<path d="${path(actPts, actGap)}" fill="none" stroke="var(--pvs-measure)" stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round"/>` : ""}
        ${now}
        <line class="fc-xh" x1="-10" y1="${PAD_T}" x2="-10" y2="${PAD_T + PH}" stroke="var(--secondary-text-color)" stroke-width="1" opacity="0"/>
        <rect class="lhit" x="${PAD_L}" y="${PAD_T}" width="${PW}" height="${PH}" fill="transparent"/>
      </svg></div>
      <div class="pvs-legend">
        ${unPts.length ? `<span class="it"><span class="sw" style="background:var(--pvs-model-ghost)"></span>${t(hass, "fc_unshaded")}</span>` : ""}
        <span class="it"><span class="sw" style="background:var(--pvs-model)"></span>${t(hass, "fc_forecast")}</span>
        ${actPts.length ? `<span class="it"><span class="sw" style="background:var(--pvs-measure)"></span>${t(hass, "fc_actual")}</span>` : ""}
      </div>`);
  }

  _rows() {
    // -> [{ms, potential, unshaded}] across the configured window, sparse.
    const hass = this._hass, cfg = this._config, r = this._resolved;
    const st = hass.states[cfg.entity];
    const own = st?.attributes?.forecast ?? [];
    let rows = own;
    if (r?.kind === "plant") {
      const extra = [r.tomorrowId, r.dayAfterId]
        .map((id) => (id && hass.states[id]?.attributes?.forecast) || []);
      rows = [...own, ...extra.flat()];
    }
    const startMs = localMidnightMs(hass, Date.now());
    const endMs = startMs + (cfg.days ?? 2) * 24 * 3600000 + 3600000; // DST slack
    const out = [];
    const seen = new Set();
    for (const row of rows) {
      const ms = new Date(row.datetime).getTime();
      if (ms < startMs || ms >= endMs || seen.has(ms)) continue;
      seen.add(ms);
      out.push({ ms, potential: row.potential_kwh, unshaded: row.unshaded_kwh });
    }
    out.sort((a, b) => a.ms - b.ms);
    return out;
  }

  _render() {
    const hass = this._hass, cfg = this._config;
    if (!hass || !cfg) return;
    // an open calendar is an input in this shadow root: rebuilding it on a
    // sensor tick would close the calendar mid-pick
    if (this.shadowRoot.activeElement?.matches?.("input[data-pick]")) {
      this._renderHeld = true;
      return;
    }
    const card = (inner) => {
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${FC_CSS}</style><ha-card>${inner}<div class="pvs-tip"></div></ha-card>`;
      this._wire();
    };
    if (!cfg.entity) return card(problemHTML(hass, { reason: t(hass, "no_entity_config") }));
    const st = hass.states[cfg.entity];
    if (!st) return card(problemHTML(hass, { reason: t(hass, "entity_missing", { entity: cfg.entity }) }));
    const need = requireFeatures(st, ["forecast_list", "unshaded"]);
    if (!need.ok) return card(problemHTML(hass, { entity: cfg.entity, missing: need.missing }));
    this._resolve();
    const r = this._resolved || { kind: "plant", producedIds: [] };
    const line = (cfg.style ?? "bars") === "line";

    const day = this._viewDay();
    if (day) {
      if (this._past?.dayKey !== day || this._past.resolved !== r) this._loadPast(day);
      if (!this._past?.data) {
        const title = cfg.title ?? st.attributes.friendly_name?.replace(/ (Prognose heute|Forecast today)$/i, "") ?? cfg.entity;
        const body = this._past?.problem
          ? problemHTML(hass, { reason: t(hass, "hist_failed", { error: esc(this._past.problem) }) })
          : `<div class="pvs-sub">…</div>`;
        return card(`<div class="pvs-head"><span class="pvs-title">${esc(title)}</span>${this._stepHTML(day)}</div>${body}`);
      }
      const src = this._pastSource(r, day);
      return line ? this._renderLine(card, st, r, src) : this._renderBars(card, st, r, src);
    }
    return line ? this._renderLine(card, st, r, this._liveSource(true))
      : this._renderBars(card, st, r, this._liveSource(false));
  }

  _renderBars(card, st, r, src) {
    const hass = this._hass, cfg = this._config;
    const isGroup = r.kind === "group";
    const showUnshaded = (cfg.show_unshaded ?? true) && !isGroup;
    const showActual = (cfg.show_actual ?? true);
    const step = this._stepHTML(src.past ? src.day : null);

    const rows = src.rows;
    const title = cfg.title ?? (src.past
      ? st.attributes.friendly_name?.replace(/ (Prognose heute|Forecast today)$/i, "")
      : st.attributes.friendly_name) ?? cfg.entity;
    if (!rows.length && !(src.past && src.actuals.size)) {
      return card(`<div class="pvs-head"><span class="pvs-title">${esc(title)}</span>${step}</div>
        ${withheldHTML(t(hass, src.past ? "hist_no_data" : "fc_no_hours"))}`);
    }

    // ---- slot grid: hourly, contiguous from first to last covered day ----
    const { startMs, days, nowMs } = src;
    const nSlots = Math.round(src.windowMs / 3600000);
    const byMs = new Map(rows.map((x) => [x.ms, x]));
    const actuals = src.actuals;
    const curHourMs = src.past ? -1 : nowMs - (nowMs % 3600000);

    // daylight span per day (first..last covered hour) for gap ticks
    const dayCover = new Map();
    for (const x of rows) {
      const dk = localParts(hass, x.ms).dayKey;
      const c = dayCover.get(dk) ?? { min: Infinity, max: -Infinity };
      c.min = Math.min(c.min, x.ms); c.max = Math.max(c.max, x.ms);
      dayCover.set(dk, c);
    }

    let maxV = 0;
    for (const x of rows) maxV = Math.max(maxV, (showUnshaded ? x.unshaded : null) ?? x.potential);
    if (actuals) for (const v of actuals.values()) maxV = Math.max(maxV, v);
    const yMax = niceMax(maxV * 1.05);

    const SW = 13, PAD_L = 30, PAD_R = 4, PAD_T = 8, PAD_B = 24, PH = 130;
    const W = PAD_L + nSlots * SW + PAD_R, H = PAD_T + PH + PAD_B;
    const yOf = (v) => PAD_T + PH - (v / yMax) * PH;

    let bars = "", hits = "", seps = "", labels = "";
    for (let i = 0; i < nSlots; i++) {
      const ms = startMs + i * 3600000;
      const x0 = PAD_L + i * SW;
      const lp = localParts(hass, ms);
      if (lp.hour === 0 && i > 0) {
        seps += `<line class="grid" x1="${x0}" y1="${PAD_T}" x2="${x0}" y2="${PAD_T + PH}"/>`;
      }
      if (lp.hour === 12) {
        labels += `<text x="${x0}" y="${H - 9}" text-anchor="middle">${fmtWeekday(hass, ms)} ${fmtDayShort(hass, ms)}</text>`;
      }
      const row = byMs.get(ms);
      const act = actuals?.get(ms);
      const isPast = ms + 3600000 <= nowMs, isCur = ms === curHourMs;
      const cov = dayCover.get(lp.dayKey);
      if (!row && cov && ms > cov.min && ms < cov.max) {
        // uncovered daylight hour: hatch tick, visually distinct from a zero bar
        bars += `<rect x="${x0 + 1.5}" y="${PAD_T + PH - 4}" width="${SW - 3}" height="4"
          fill="url(#pvs-hatch-fc)" data-gap="1"/>`;
      }
      if (row) {
        if (showUnshaded && row.unshaded > 0) {
          bars += `<rect x="${x0 + 0.5}" y="${yOf(row.unshaded)}" width="${SW - 1}"
            height="${Math.max(0, PAD_T + PH - yOf(row.unshaded))}" rx="2"
            fill="var(--pvs-model-ghost)"/>`;
        }
        if (row.potential > 0) {
          const bw = SW * 0.62;
          bars += `<rect x="${x0 + (SW - bw) / 2}" y="${yOf(row.potential)}" width="${bw}"
            height="${Math.max(0, PAD_T + PH - yOf(row.potential))}" rx="2"
            fill="var(--pvs-model)"/>`;
        }
      }
      if (showActual && act != null && (isPast || isCur) && act > 0) {
        const aw = SW * 0.32;
        const ay = yOf(act);
        bars += `<rect x="${x0 + (SW - aw) / 2}" y="${ay}" width="${aw}"
          height="${Math.max(0, PAD_T + PH - ay)}" rx="1.5"
          fill="var(--pvs-measure)" stroke="var(--card-background-color)" stroke-width="1"/>`;
        if (isCur) {
          bars += `<rect x="${x0 + (SW - aw) / 2}" y="${ay}" width="${aw}" height="${Math.min(8, PAD_T + PH - ay)}"
            fill="url(#pvs-hatch-fc)"/>`;
        }
      }
      const da = src.dayAhead?.get(ms) ?? null;
      const payload = { ms, f: row?.potential ?? null, u: row?.unshaded ?? null,
        a: act ?? null, cur: isCur, gap: !row && cov && ms > cov.min && ms < cov.max,
        da: da?.v ?? null, daAt: da?.at ?? null };
      hits += `<rect class="hit" x="${x0}" y="${PAD_T}" width="${SW}" height="${PH}"
        fill="transparent" data-slot='${esc(JSON.stringify(payload))}'/>`;
    }

    // now marker
    if (curHourMs >= startMs && curHourMs < startMs + nSlots * 3600000) {
      const nx = PAD_L + ((nowMs - startMs) / 3600000) * SW;
      seps += `<line x1="${nx}" y1="${PAD_T - 3}" x2="${nx}" y2="${PAD_T + PH}"
        stroke="var(--pvs-measure)" stroke-width="1" stroke-dasharray="2 3" opacity="0.8"/>
        <text x="${nx}" y="${PAD_T - 1}" text-anchor="middle" fill="var(--pvs-measure)"
        style="font-size:9px">${t(hass, "now")}</text>`;
    }

    // y axis: 0, half, max
    let yaxis = "";
    for (const v of [0, yMax / 2, yMax]) {
      yaxis += `<line class="grid" x1="${PAD_L}" y1="${yOf(v)}" x2="${W - PAD_R}" y2="${yOf(v)}"/>
        <text class="axis" x="${PAD_L - 4}" y="${yOf(v) + 3}" text-anchor="end">${fmtNum(hass, v, v < 2 && v !== 0 ? 1 : 0)}</text>`;
    }

    // header chips
    const chips = [];
    const chip = (label, value, entityId, sub) => {
      if (value == null) return;
      chips.push(`<span class="pvs-chip${entityId ? " clickable" : ""}" ${entityId ? `data-more-info="${entityId}"` : ""}>
        ${label} <span class="v">${value}</span>${sub ? ` <span class="pvs-sub">${sub}</span>` : ""}</span>`);
    };
    if (src.past) {
      chip(t(hass, "fc_forecast"), src.totals.forecast == null ? null : fmtKwh(hass, src.totals.forecast, 1));
      chip(t(hass, "fc_actual"), src.totals.actual == null ? null : fmtKwh(hass, src.totals.actual, 1));
      chip(t(hass, "hist_day_ahead"), src.totals.dayAhead == null ? null : fmtKwh(hass, src.totals.dayAhead, 1));
    } else if (isGroup) {
      chip(t(hass, "today"), fmtKwh(hass, st.attributes.today_kwh, 1), cfg.entity);
      chip(t(hass, "remaining"), fmtKwh(hass, parseFloat(st.state), 1), cfg.entity);
      chip(t(hass, "tomorrow"), fmtKwh(hass, st.attributes.tomorrow_kwh, 1), cfg.entity);
    } else {
      chip(t(hass, "today"), fmtKwh(hass, parseFloat(st.state), 1), cfg.entity);
      if (r.remainingId) {
        const rs = hass.states[r.remainingId];
        chip(t(hass, "remaining"), rs ? fmtKwh(hass, parseFloat(rs.state), 1) : null, r.remainingId);
      }
      if (r.tomorrowId && days > 1) {
        const ts2 = hass.states[r.tomorrowId];
        chip(t(hass, "tomorrow"), ts2 ? fmtKwh(hass, parseFloat(ts2.state), 1) : null, r.tomorrowId);
      }
    }

    const notes = [];
    if (isGroup && (cfg.show_unshaded ?? true)) {
      notes.push(`<div class="fc-note">${t(hass, "fc_group_unshaded")}</div>`);
    }
    if (src.past && src.dayAheadGone) {
      notes.push(`<div class="fc-note">${t(hass, "hist_day_ahead_gone")}</div>`);
    }
    if (!src.past && this._actualsProblem === "fc_actual_unresolved") {
      notes.push(`<div class="fc-note">${t(hass, "fc_actual_unresolved")}</div>`);
    } else if (!src.past && this._actualsProblem === "stats_unavailable") {
      notes.push(`<div class="fc-note">${t(hass, "stats_unavailable", { entity: r.producedIds?.join(", ") ?? "?" })}</div>`);
    }

    const hasUnshaded = showUnshaded && rows.some((x) => x.unshaded != null);
    const legend = `<div class="pvs-legend">
      ${hasUnshaded ? `<span class="it"><span class="sw" style="background:var(--pvs-model-ghost)"></span>${t(hass, "fc_unshaded")}</span>` : ""}
      <span class="it"><span class="sw" style="background:var(--pvs-model)"></span>${t(hass, "fc_forecast")}</span>
      ${showActual ? `<span class="it"><span class="sw" style="background:var(--pvs-measure)"></span>${t(hass, "fc_actual")}</span>` : ""}
      <span class="it"><svg width="14" height="10"><rect width="14" height="10" rx="2" fill="url(#pvs-hatch-fcl)"/></svg>${t(hass, "fc_gap")}</span>
    </div>`;

    card(`
      <div class="pvs-head"><span class="pvs-title">${esc(title)}</span>${chips.join("")}${step}</div>
      ${notes.join("")}
      <div class="fc-wrap"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="aspect-ratio:${W}/${H}">
        <defs>${hatchPattern("pvs-hatch-fc", "var(--pvs-measure)")}${hatchPattern("pvs-hatch-fcl")}</defs>
        ${yaxis}${seps}${bars}${hits}
      </svg></div>
      ${legend}`);
  }

  _wire() {
    this._wireMoreInfo();
    if (this._wired) return;
    this._wired = true;
    this.shadowRoot.addEventListener("pointerleave", () => {
      this.shadowRoot.querySelector(".fc-xh")?.setAttribute("opacity", "0");
    });
    this.shadowRoot.addEventListener("change", (ev) => {
      const p = ev.target.closest?.("input[data-pick]");
      if (!p) return;
      const v = p.value;
      p.blur();
      this._renderHeld = false;
      this._pick(v);
    });
    // a closed calendar without a pick lets the held render through
    this.shadowRoot.addEventListener("focusout", (ev) => {
      if (!ev.target.matches?.("input[data-pick]") || !this._renderHeld) return;
      this._renderHeld = false;
      setTimeout(() => this._render(), 0);
    });
    this.shadowRoot.addEventListener("click", (ev) => {
      const p = ev.target.closest?.("input[data-pick]");
      if (p) {
        ev.stopPropagation();
        // Chrome opens the calendar only from its icon or showPicker();
        // the others have opened it already and may refuse a second call
        try { p.showPicker?.(); } catch (_) { /* native open is enough */ }
        return;
      }
      const b = ev.target.closest?.("[data-step]");
      if (!b || b.disabled) return;
      ev.stopPropagation();
      this._step(parseInt(b.getAttribute("data-step"), 10));
    });
    wireTooltip(this, {
      selector: ".hit,.lhit",
      content: (el, ev) => {
        if (el.classList.contains("lhit")) return this._lineTip(ev);
        const hass = this._hass;
        const d = JSON.parse(el.getAttribute("data-slot"));
        if (d.f == null && d.a == null) {
          return d.gap ? `<div class="h">${fmtHour(hass, d.ms)}</div><div>${t(hass, "fc_gap")}</div>` : null;
        }
        const shadow = (d.u > 0 && d.f != null) ? (1 - d.f / d.u) * 100 : null;
        const delta = (d.a != null && d.f != null) ? d.a - d.f : null;
        return `<div class="h">${fmtWeekday(hass, d.ms)} ${fmtHour(hass, d.ms)}</div>
          ${d.f != null ? `<div class="r"><span class="k">${t(hass, "fc_forecast")}</span><span class="v">${fmtKwh(hass, d.f)}</span></div>` : ""}
          ${d.u != null && d.u !== d.f ? `<div class="r"><span class="k">${t(hass, "fc_unshaded")}</span><span class="v">${fmtKwh(hass, d.u)}</span></div>` : ""}
          ${shadow != null && shadow > 0.5 ? `<div class="r"><span class="k">${t(hass, "fc_known_shadow")}</span><span class="v">${fmtNum(hass, shadow, 0)} %</span></div>` : ""}
          ${d.a != null ? `<div class="r"><span class="k">${t(hass, "fc_actual")}</span><span class="v">${fmtKwh(hass, d.a)}</span></div>` : ""}
          ${delta != null && !d.cur ? `<div class="r"><span class="k">${t(hass, "fc_delta")}</span><span class="v">${fmtSigned(hass, delta)} kWh</span></div>` : ""}
          ${d.da != null ? `<div class="r"><span class="k">${t(hass, "hist_day_ahead")}</span><span class="v">${fmtKwh(hass, d.da)}</span></div>` : ""}
          ${d.da != null && d.daAt ? this._issuedNote(d.daAt) : ""}
          ${d.cur ? `<div class="pvs-sub">${t(hass, "in_progress", { min: new Date().getMinutes() })}</div>` : ""}`;
      },
    });
  }

  _lineTip(ev) {
    const hass = this._hass;
    const g = this._lineGeom, s = this._lineSeries;
    if (!g || !s) return null;
    const svg = this.shadowRoot.querySelector("svg.fc-line");
    const rect = svg.getBoundingClientRect();
    const vx = (ev.clientX - rect.left) / rect.width * g.W;
    const ms = g.startMs + (vx - g.PAD_L) / g.PW * g.windowMs;
    const hourMs = ms - (ms % 3600000);
    const fc = s.fc.get(hourMs);
    let act = null, best = 21 * 60000;
    for (const p of s.act) {
      const d = Math.abs(p.ms - ms);
      if (d < best) { best = d; act = p; }
    }
    const xh = svg.querySelector(".fc-xh");
    if (xh) { xh.setAttribute("x1", vx); xh.setAttribute("x2", vx); xh.setAttribute("opacity", "0.4"); }
    if (!fc && !act) return null;
    const shadow = fc && fc.unshaded > 0 ? (1 - fc.potential / fc.unshaded) * 100 : null;
    return `<div class="h">${fmtWeekday(hass, ms)} ${fmtHour(hass, ms)}</div>
      ${fc ? `<div class="r"><span class="k">${t(hass, "fc_forecast")}</span><span class="v">${fmtNum(hass, fc.potential * 1000, 0)} W</span></div>` : ""}
      ${fc && fc.unshaded !== fc.potential ? `<div class="r"><span class="k">${t(hass, "fc_unshaded")}</span><span class="v">${fmtNum(hass, fc.unshaded * 1000, 0)} W</span></div>` : ""}
      ${shadow != null && shadow > 0.5 ? `<div class="r"><span class="k">${t(hass, "fc_known_shadow")}</span><span class="v">${fmtNum(hass, shadow, 0)} %</span></div>` : ""}
      ${act ? `<div class="r"><span class="k">${t(hass, "fc_actual")}</span><span class="v">${fmtNum(hass, act.v, 0)} W</span></div>` : ""}
      ${s.dayAhead?.get(hourMs) ? `<div class="r"><span class="k">${t(hass, "hist_day_ahead")}</span><span class="v">${fmtNum(hass, s.dayAhead.get(hourMs).v * 1000, 0)} W</span></div>` : ""}
      ${s.hourlyFallback && act ? `<div class="pvs-sub">${t(hass, "fc_hourly_fallback")}</div>` : ""}`;
  }

  // The day-ahead figure is the run of the evening before at the issue hour;
  // where that run was missing the integration fell back to an earlier one,
  // and the tooltip says so rather than claiming the stricter provenance.
  _issuedNote(iso) {
    const hass = this._hass, ms = Date.parse(iso);
    if (!Number.isFinite(ms)) return "";
    const issueHour = this._past?.data?.issue_hour_local;
    const lp = localParts(hass, ms);
    if (issueHour != null && lp.hour === issueHour && lp.minute === 0) return "";
    return `<div class="pvs-sub">${t(hass, "hist_issued_earlier", { when: `${fmtWeekday(hass, ms)} ${fmtHour(hass, ms)}` })}</div>`;
  }

  static getConfigElement() { return document.createElement("pvstrings-forecast-editor"); }
  static getStubConfig(hass, entities) {
    const guess = (entities ?? []).find((e) =>
      Array.isArray(hass.states[e]?.attributes?.forecast));
    return { entity: guess ?? "", days: 2 };
  }
}

const FC_CSS = `
  .fc-wrap svg { width: 100%; height: auto; display: block; }
  .fc-note { font-size: 11px; color: var(--secondary-text-color); margin: 2px 0 6px; }
  .hit, .lhit { cursor: crosshair; }
  .fc-hero { display: flex; align-items: flex-start; gap: 14px; margin-bottom: 8px; }
  .fc-hero-mid { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: center; gap: 6px; align-self: center; }
  .fc-hero-mid .fc-hero-title { flex: none; max-width: 100%; }
  .fc-hero-title {
    flex: 1; text-align: center; align-self: center;
    font-size: 13px; font-weight: 600; color: var(--secondary-text-color);
    letter-spacing: 0.2px; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .fc-hero-item { display: flex; flex-direction: column; gap: 1px; }
  .fc-hero-item.right { text-align: right; }
  .fc-hero-item.clickable { cursor: pointer; }
  .fc-hero-item .hv { font-family: var(--pvs-mono); font-variant-numeric: tabular-nums; font-size: 24px; font-weight: 700; line-height: 1.1; }
  .fc-hero-item .hu { font-size: 12px; font-weight: 500; margin-left: 3px; opacity: 0.75; }
  .fc-hero-item .hl { font-size: 11px; color: var(--secondary-text-color); }
  .fc-hero.wide .hv { font-size: 32px; }
  .fc-hero.wide .hu { font-size: 14px; }
  .fc-hero.wide .fc-hero-title { font-size: 15px; }
`;

/* ======================= SECTION: CARD:CONVERSION ======================== */

// DC potential vs converted output (AC or battery charge) for one group.
// Semantics per the integration's contract: the output is hardware
// potential (capped at the AC rating, never at regulatory limits),
// clipping is a hardware cap and not a conversion loss, and a neutral
// curve means "unconverted" — not a measured 0 % loss. The per-hour ratio
// strip omits hours whose DC is too small for a meaningful quotient
// (hatched, rule 1: absence is shown, never faked as a value).
// Curve-source label for chip and nerd table. fixed_factors carries the
// applied multiplier (conversion_factor, e.g. 0.9312 = MPPT 0.97 × charge
// 0.96) — showing it is the point; without it, the label alone.
function curveLabel(hass, source, factor, prior) {
  if (!source) return null;
  if (source === "fixed_factors")
    return t(hass, "conv_curve_fixed_factors") +
      (factor != null ? ` × ${fmtNum(hass, factor, 3)}` : "");
  if (source === "learned") {
    const priorTxt = ["datasheet", "custom"].includes(prior)
      ? t(hass, "conv_curve_" + prior) : null;
    return t(hass, "conv_curve_learned") + (priorTxt
      ? ` <span class="pvs-sub">· ${t(hass, "conv_curve_prior", { prior: priorTxt })}</span>` : "");
  }
  return ["datasheet", "custom", "neutral"].includes(source)
    ? t(hass, "conv_curve_" + source) : esc(source);
}

class PvsConversionCard extends PvsBaseCard {
  watchedEntities() {
    return [this._config?.entity, this._config?.dc_entity].filter(Boolean);
  }
  getCardSize() { return 4; }
  getGridOptions() { return { columns: "full", rows: "auto" }; }

  _render() {
    const hass = this._hass, cfg = this._config;
    if (!hass || !cfg) return;
    const card = (inner) => {
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${FC_CSS}</style><ha-card>${inner}<div class="pvs-tip"></div></ha-card>`;
      this._wire();
    };
    if (!cfg.entity || !cfg.dc_entity) return card(problemHTML(hass, { reason: t(hass, "no_entity_config") }));
    const outSt = hass.states[cfg.entity], dcSt = hass.states[cfg.dc_entity];
    if (!outSt) return card(problemHTML(hass, { reason: t(hass, "entity_missing", { entity: cfg.entity }) }));
    if (!dcSt) return card(problemHTML(hass, { reason: t(hass, "entity_missing", { entity: cfg.dc_entity }) }));

    const a = outSt.attributes;
    const path = a.output_path === "storage" ? "storage" : "direct";
    const outLabel = t(hass, "conv_out_" + path);
    const title = cfg.title ?? a.friendly_name ?? cfg.entity;
    // Coordinator not ready is "not yet", never a contract error.
    if (outSt.state === "unavailable" || dcSt.state === "unavailable") {
      return card(`<div class="pvs-head"><span class="pvs-title">${esc(title)}</span></div>
        ${withheldHTML(t(hass, "conv_not_ready"))}`);
    }
    for (const [st, id, feats] of [[outSt, cfg.entity, ["forecast_list", "conv_output"]],
      [dcSt, cfg.dc_entity, ["forecast_list"]]]) {
      const need = requireFeatures(st, feats);
      if (!need.ok) return card(problemHTML(hass, { entity: id, missing: need.missing }));
    }

    // ---- today's hours from both lists ----
    const startMs = localMidnightMs(hass, Date.now());
    const endMs = startMs + 25 * 3600000; // DST slack
    const pick = (st) => {
      const m = new Map();
      for (const row of st.attributes.forecast ?? []) {
        const ms = new Date(row.datetime).getTime();
        if (ms >= startMs && ms < endMs && !m.has(ms)) m.set(ms, row.potential_kwh);
      }
      return m;
    };
    const dc = pick(dcSt), out = pick(outSt);

    // ---- header chips ----
    const neutral = a.curve_source === "neutral";
    const dcToday = dcSt.attributes.today_kwh;
    const outToday = a.today_kwh;
    const chips = [];
    if (!neutral && outToday != null && dcToday > 0) {
      chips.push(`<span class="pvs-chip clickable" data-more-info="${cfg.entity}"
        title="${esc(t(hass, "conv_eff_tip_" + path))}">
        ${t(hass, "conv_eff_today")} <span class="v">${fmtNum(hass, (outToday / dcToday) * 100, 1)} %</span></span>`);
    }
    if (a.curve_source) {
      chips.push(`<span class="pvs-chip clickable" data-more-info="${cfg.entity}"
        title="${esc(neutral ? t(hass, "conv_neutral_note")
          : a.curve_source === "fixed_factors" ? t(hass, "conv_fixed_factors_tip")
          : t(hass, "more_info"))}">${curveLabel(hass, a.curve_source, a.conversion_factor, a.curve_prior)}</span>`);
    }
    if (!neutral && a.clipped_kwh > 0) {
      chips.push(`<span class="pvs-chip warn" title="${esc(t(hass, "conv_clipped_tip"))}">
        <span class="ico">⚠︎</span> ${t(hass, "conv_clipped", { v: fmtNum(hass, a.clipped_kwh, 2) })}</span>`);
    }
    const heroDc = dcToday != null
      ? `<div class="fc-hero-item clickable" data-more-info="${cfg.dc_entity}">
          <span class="hv" style="color:var(--secondary-text-color)">${fmtNum(hass, dcToday, 1)}<span class="hu">kWh</span></span>
          <span class="hl">${t(hass, "conv_hero_dc")}</span></div>` : "";
    const heroOut = outToday != null
      ? `<div class="fc-hero-item right clickable" data-more-info="${cfg.entity}">
          <span class="hv" style="color:var(--pvs-model)">${fmtNum(hass, outToday, 1)}<span class="hu">kWh</span></span>
          <span class="hl">${t(hass, "conv_hero_" + path)}</span></div>` : "";
    const head = `
      <div class="fc-hero">${heroDc}<span class="fc-hero-title">${esc(title)}</span>${heroOut}</div>
      ${chips.length ? `<div class="pvs-head" style="justify-content:flex-start">${chips.join("")}</div>` : ""}`;

    if (!dc.size) return card(head + withheldHTML(t(hass, "fc_no_hours")));

    // ---- geometry: hourly bars + ratio strip in one SVG ----
    const nSlots = 24;
    const SW = 26, PAD_L = 34, PAD_R = 6, PAD_T = 8, PH = 108;
    const GAP = 12, STRIP_H = 30, PAD_B = 20;
    const W = PAD_L + nSlots * SW + PAD_R;
    const H = PAD_T + PH + GAP + STRIP_H + PAD_B;
    const STRIP_Y = PAD_T + PH + GAP;
    let maxV = 0;
    for (const v of dc.values()) maxV = Math.max(maxV, v ?? 0);
    const yMax = niceMax(maxV * 1.05);
    const yOf = (v) => PAD_T + PH - (v / yMax) * PH;
    // materiality: below this DC an hourly quotient is noise, not signal
    const thresh = Math.max(0.05, 0.04 * maxV);

    let bars = "", strip = "", hits = "", labels = "";
    for (let i = 0; i < nSlots; i++) {
      const ms = startMs + i * 3600000;
      const x0 = PAD_L + i * SW;
      const lp = localParts(hass, ms);
      if (lp.hour % 4 === 0) {
        labels += `<text class="axis" x="${x0 + SW / 2}" y="${H - 6}" text-anchor="middle">${fmtHour(hass, ms)}</text>`;
      }
      const d = dc.get(ms), o = out.get(ms);
      if (d != null && d > 0) {
        bars += `<rect x="${x0 + 1}" y="${yOf(d)}" width="${SW - 2}" height="${PAD_T + PH - yOf(d)}"
          fill="var(--pvs-model-ghost)" rx="1"/>`;
      }
      if (o != null && o > 0) {
        bars += `<rect x="${x0 + 1}" y="${yOf(o)}" width="${SW - 2}" height="${PAD_T + PH - yOf(o)}"
          fill="var(--pvs-model)" rx="1"/>`;
      }
      // ratio strip: honest — hatched where DC is immaterial
      const material = d != null && d >= thresh;
      const ratio = material && o != null ? o / d : null;
      if (!neutral) {
        if (ratio != null) {
          const rh = Math.min(1, ratio) * STRIP_H;
          strip += `<rect x="${x0 + 1}" y="${STRIP_Y + STRIP_H - rh}" width="${SW - 2}" height="${rh}"
            fill="var(--pvs-model)" opacity="0.45" rx="1"/>`;
        } else if (d != null && d > 0) {
          strip += `<rect x="${x0 + 1}" y="${STRIP_Y}" width="${SW - 2}" height="${STRIP_H}"
            fill="url(#pvs-conv-hatch)" stroke="none"/>`;
        }
      }
      const tip = { h: lp.hour, dc: d ?? null, out: o ?? null,
        ratio: ratio != null ? ratio : null, low: d != null && d > 0 && !material };
      hits += `<rect x="${x0}" y="${PAD_T}" width="${SW}" height="${H - PAD_T - PAD_B}"
        fill="transparent" data-conv='${esc(JSON.stringify(tip))}'/>`;
    }

    // axes: kWh ticks for the bars, 100 % line for the strip
    let grid = "";
    for (const v of [0, yMax / 2, yMax]) {
      grid += `<line class="grid" x1="${PAD_L}" y1="${yOf(v)}" x2="${W - PAD_R}" y2="${yOf(v)}"/>
        <text class="axis" x="${PAD_L - 5}" y="${yOf(v) + 3}" text-anchor="end">${fmtNum(hass, v, 1)}</text>`;
    }
    if (!neutral) {
      grid += `<line class="grid" x1="${PAD_L}" y1="${STRIP_Y}" x2="${W - PAD_R}" y2="${STRIP_Y}"/>
        <line class="grid" x1="${PAD_L}" y1="${STRIP_Y + STRIP_H}" x2="${W - PAD_R}" y2="${STRIP_Y + STRIP_H}"/>
        <text class="axis" x="${PAD_L - 5}" y="${STRIP_Y + 4}" text-anchor="end">100%</text>
        <text class="axis" x="${PAD_L - 5}" y="${STRIP_Y + STRIP_H + 3}" text-anchor="end">0</text>`;
    }

    const notes = [];
    if (neutral) notes.push(`<div class="fc-note">${t(hass, "conv_neutral_note")}</div>`);
    if (a.note) notes.push(`<div class="fc-note">${esc(a.note)}</div>`);

    card(`
      ${head}
      ${notes.join("")}
      <div class="fc-wrap"><svg class="fc-line" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="aspect-ratio:${W}/${H}">
        <defs>${hatchPattern("pvs-conv-hatch")}</defs>
        ${grid}${bars}${strip}${labels}${hits}
      </svg></div>
      <div class="pvs-legend">
        <span class="it"><span class="sw" style="background:var(--pvs-model-ghost)"></span>${t(hass, "conv_dc")}</span>
        <span class="it"><span class="sw" style="background:var(--pvs-model)"></span>${outLabel}</span>
        ${neutral ? "" : `<span class="it"><span class="sw" style="background:var(--pvs-model);opacity:0.45"></span>${t(hass, "conv_strip", { out: outLabel })}</span>`}
      </div>`);
  }

  _wire() {
    this._wireMoreInfo();
    if (this._wired) return;
    this._wired = true;
    wireTooltip(this, {
      selector: "[data-conv]",
      content: (el) => {
        const hass = this._hass;
        const c = JSON.parse(el.getAttribute("data-conv"));
        const outLabel = t(hass, "conv_out_" +
          (this._hass?.states[this._config?.entity]?.attributes?.output_path === "storage" ? "storage" : "direct"));
        return `<div class="h">${String(c.h).padStart(2, "0")}:00–${String((c.h + 1) % 24).padStart(2, "0")}:00</div>
          ${c.dc != null ? `<div class="r"><span class="k">${t(hass, "conv_dc")}</span><span class="v">${fmtNum(hass, c.dc, 2)} kWh</span></div>` : ""}
          ${c.out != null ? `<div class="r"><span class="k">${outLabel}</span><span class="v">${fmtNum(hass, c.out, 2)} kWh</span></div>` : ""}
          ${c.ratio != null ? `<div class="r"><span class="k">${outLabel} / DC</span><span class="v">${fmtNum(hass, c.ratio * 100, 1)} %</span></div>` : ""}
          ${c.low ? `<div class="pvs-sub">${t(hass, "conv_low_dc")}</div>` : ""}`;
      },
    });
  }

  static getConfigElement() {
    return document.createElement("pvstrings-conversion-editor");
  }
}

/* ======================== SECTION: CARD:NOWCAST =========================== */

// The plant forecast reacting to its own irradiance sensor: how clear the
// last quarter hour measured, how heavily that is blended into the coming
// intervals, and how fast it fades back to the provider's forecast.
// Inactive is the normal state at night and on plants without a sensor —
// then the REASON is the content, not an error.
class PvsNowcastCard extends PvsBaseCard {
  getCardSize() { return 3; }
  getGridOptions() { return { columns: "full", rows: "auto" }; }

  _render() {
    const hass = this._hass, cfg = this._config;
    if (!hass || !cfg) return;
    const card = (inner) => {
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${FC_CSS}${NC_CSS}</style><ha-card>${inner}<div class="pvs-tip"></div></ha-card>`;
      this._wireMoreInfo();
      if (!this._wired) { this._wired = true; wireTooltip(this); }
    };
    if (!cfg.entity) return card(problemHTML(hass, { reason: t(hass, "no_entity_config") }));
    const st = hass.states[cfg.entity];
    if (!st) return card(problemHTML(hass, { reason: t(hass, "entity_missing", { entity: cfg.entity }) }));
    const need = requireFeatures(st, ["nowcast"]);
    if (!need.ok) return card(problemHTML(hass, { entity: cfg.entity, missing: need.missing }));

    const a = st.attributes;
    const on = !!a.nowcast_active;
    const title = cfg.title ?? t(hass, "nc_title");
    const stateChip = `<span class="pvs-chip ${on ? "on" : "dim"}">
      <span class="dot${on ? " live" : ""}"></span>${t(hass, on ? "nc_active" : "nc_inactive")}</span>`;
    const head = (extra = "") => `<div class="pvs-head">
      <span class="pvs-title clickable" data-more-info="${cfg.entity}">${esc(title)}</span>
      ${stateChip}${extra}${helpChip("nowcast")}</div>`;

    // ---- inactive: the reason is the whole content ------------------------
    if (!on) {
      const r = a.nowcast_reason;
      const known = ["no_source", "no_measurement", "too_dark", "thin_window",
        "stale", "frozen_sensor", "learning_off"].includes(r);
      return card(head() + withheldHTML(known ? t(hass, "nc_reason_" + r)
        : t(hass, "nc_reason_other", { reason: esc(r ?? "?") })));
    }

    // ---- active -----------------------------------------------------------
    const kt = a.nowcast_kt, w0 = a.nowcast_weight_now ?? 0;
    const half = a.nowcast_halflife_min ?? null;
    const sky = ["calm", "broken"].includes(a.nowcast_sky) ? a.nowcast_sky : "unknown";
    const trust = a.nowcast_trust;
    const chips = [`<span class="pvs-chip">${t(hass, "nc_sky_" + sky)}</span>`];
    if (half != null) {
      chips.push(`<span class="pvs-chip">${t(hass, "nc_halflife")} <span class="v">${fmtNum(hass, half, 0)}</span> min</span>`);
    }
    if (trust != null && trust < 1) {
      chips.push(`<span class="pvs-chip" title="${esc(t(hass, "nc_trust_tip"))}">
        ${t(hass, "nc_trust")} <span class="v">${fmtNum(hass, trust * 100, 0)} %</span></span>`);
    }

    // clearness scale: 0 (dark) .. 1.1 (brighter than the clear-sky model)
    const KT_MAX = 1.1;
    const pctOf = (v) => Math.max(0, Math.min(1, v / KT_MAX)) * 100;
    const ktPct = kt != null ? pctOf(kt) : null;
    // What the source says for now, on the same scale: without it the
    // measured figure has nothing to be read against. Sensor orange,
    // forecast blue — as on every other card.
    const fcW = a.forecast_wm2, csW = a.clearsky_wm2;
    const fcKt = Number.isFinite(fcW) && Number.isFinite(csW) && csW > 0 ? fcW / csW : null;
    let vs = "";
    if (kt != null && fcKt != null) {
      const d = kt - fcKt;
      const key = Math.abs(d) < 0.05 ? "nc_vs_same" : d > 0 ? "nc_vs_brighter" : "nc_vs_darker";
      const sign = d >= 0 ? "+" : "−";
      vs = `<span class="nc-fc" title="${esc(t(hass, "nc_fc_tip"))}">
          <span class="nc-sw fc"></span>${t(hass, "nc_fc")} <b>${fmtNum(hass, fcKt, 2)}</b></span>
        <span class="pvs-chip">${t(hass, key)}${key === "nc_vs_same" ? ""
          : ` <span class="v">${sign}${fmtNum(hass, Math.abs(d), 2)}</span>`}</span>`;
    }
    const ktRow = kt == null ? "" : `
      <div class="nc-kt">
        <div class="nc-lbl"><span class="nc-sw ms"></span>${t(hass, "nc_kt")}<span class="nc-big">${fmtNum(hass, kt, 2)}</span>${vs}</div>
        <div class="nc-scale">
          <div class="nc-fill" style="width:${ktPct.toFixed(1)}%"></div>
          <div class="nc-mark" style="left:${ktPct.toFixed(1)}%"></div>
          ${fcKt != null ? `<div class="nc-fcmark" style="left:${pctOf(fcKt).toFixed(1)}%"></div>` : ""}
          ${[0.25, 0.5, 0.75, 1].map((v) => `<div class="nc-tick" style="left:${(v / KT_MAX * 100).toFixed(1)}%"></div>`).join("")}
        </div>
        <div class="nc-scale-ax">
          ${[0, 0.5, 1].map((v) => `<span style="left:${(v / KT_MAX * 100).toFixed(1)}%">${fmtNum(hass, v, 1)}</span>`).join("")}
        </div>
      </div>`;

    // ---- decay: w(t) = w0 * 2^(-t/halflife), cut at the two-hour reach ----
    // Derived from the two published numbers, not published per interval —
    // the note under the chart says so.
    const REACH = 120;
    const PAD_L = 34, PAD_R = 10, PAD_T = 10, PAD_B = 20, PW = 560, PH = 92;
    const W = PAD_L + PW + PAD_R, H = PAD_T + PH + PAD_B;
    const xOf = (m) => PAD_L + (m / REACH) * PW;
    const yOf = (v) => PAD_T + PH - Math.max(0, Math.min(1, v)) * PH;
    const wAt = (m) => (half ? w0 * Math.pow(2, -m / half) : (m === 0 ? w0 : 0));
    let dPath = "", area = "";
    for (let m = 0; m <= REACH; m += 4) {
      const x = xOf(m).toFixed(1), y = yOf(wAt(m)).toFixed(1);
      dPath += `${m === 0 ? "M" : "L"}${x} ${y}`;
    }
    area = `${dPath}L${xOf(REACH).toFixed(1)} ${yOf(0).toFixed(1)}L${xOf(0).toFixed(1)} ${yOf(0).toFixed(1)}Z`;
    let grid = "";
    for (const v of [0, 0.5, 1]) {
      grid += `<line class="grid" x1="${PAD_L}" y1="${yOf(v)}" x2="${W - PAD_R}" y2="${yOf(v)}"/>
        <text class="axis" x="${PAD_L - 5}" y="${yOf(v) + 3}" text-anchor="end">${fmtNum(hass, v * 100, 0)}</text>`;
    }
    grid += `<text class="axis" x="${PAD_L - 5}" y="${PAD_T - 2}" text-anchor="end" style="font-size:8.5px">%</text>`;
    for (const m of [0, 30, 60, 90, 120]) {
      grid += `<text class="axis" x="${xOf(m)}" y="${H - 6}" text-anchor="middle">${m}</text>`;
    }
    grid += `<text class="axis" x="${PAD_L + PW / 2}" y="${H - 6}" text-anchor="middle" style="opacity:0"> </text>`;
    let halfMark = "";
    if (half != null && half <= REACH) {
      halfMark = `<line x1="${xOf(half).toFixed(1)}" y1="${PAD_T}" x2="${xOf(half).toFixed(1)}" y2="${PAD_T + PH}"
          stroke="var(--pvs-model)" stroke-width="1" stroke-dasharray="3 2" opacity="0.7"/>
        <text class="axis" x="${(xOf(half) + 4).toFixed(1)}" y="${PAD_T + 9}">${t(hass, "nc_halflife")}</text>`;
    }

    const stats = [];
    if (a.nowcast_intervals != null) stats.push(t(hass, "nc_intervals", { n: a.nowcast_intervals }));
    if (a.nowcast_spread != null) stats.push(`${t(hass, "nc_spread")} ± ${fmtNum(hass, a.nowcast_spread, 3)}`);

    card(`
      ${head(chips.join(""))}
      ${ktRow}
      <div class="nc-wsub">${t(hass, "nc_weight")} <b>${fmtNum(hass, w0 * 100, 0)} %</b></div>
      <div class="fc-wrap"><svg class="fc-line" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="aspect-ratio:${W}/${H}">
        ${grid}
        <path d="${area}" fill="var(--pvs-model)" opacity="0.16"/>
        <path d="${dPath}" fill="none" stroke="var(--pvs-model)" stroke-width="2.2" stroke-linejoin="round"/>
        ${halfMark}
        <circle cx="${xOf(0).toFixed(1)}" cy="${yOf(w0).toFixed(1)}" r="3.4" fill="var(--pvs-model)"/>
      </svg></div>
      <div class="nc-ax">${t(hass, "nc_axis_min")}</div>
      ${stats.length ? `<div class="nc-stats">${stats.join(" · ")}</div>` : ""}
      <div class="kv-note dim">${t(hass, "nc_decay_note")}</div>`);
  }

  static getConfigElement() { return document.createElement("pvstrings-chain-editor"); }
  static getStubConfig() { return { entity: "" }; }
}

const NC_CSS = `
  .pvs-chip .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block;
    margin-right: 5px; background: var(--secondary-text-color); }
  .pvs-chip .dot.live { background: var(--pvs-model);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--pvs-model) 25%, transparent); }
  .nc-kt { margin: 6px 0 2px; }
  .nc-lbl { font-size: 11px; color: var(--secondary-text-color); display: flex;
    align-items: baseline; gap: 8px; flex-wrap: wrap; }
  .nc-sw { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: -3px; }
  .nc-sw.ms { background: var(--pvs-measure); }
  .nc-sw.fc { background: var(--pvs-model); margin-right: 3px; }
  .nc-fc { margin-left: 8px; cursor: help; }
  .nc-fc b { color: var(--primary-text-color); font-size: 15px; font-weight: 600;
    font-variant-numeric: tabular-nums; }
  .nc-big { font-size: 24px; font-weight: 600; color: var(--primary-text-color);
    font-variant-numeric: tabular-nums; }
  .nc-scale { position: relative; height: 10px; border-radius: 5px; margin-top: 3px;
    background: linear-gradient(90deg,
      color-mix(in srgb, var(--pvs-measure) 8%, transparent),
      color-mix(in srgb, var(--pvs-measure) 20%, transparent)); overflow: hidden; }
  .nc-fill { position: absolute; inset: 0 auto 0 0; border-radius: 5px;
    background: var(--pvs-measure); opacity: 0.75; }
  .nc-fcmark { position: absolute; top: 0; width: 4px; height: 10px; border-radius: 2px;
    background: var(--pvs-model); transform: translateX(-2px);
    box-shadow: 0 0 0 1.5px var(--card-background-color); }
  .nc-mark { position: absolute; top: -2px; width: 2px; height: 14px;
    background: var(--primary-text-color); transform: translateX(-1px); }
  .nc-tick { position: absolute; top: 3px; width: 1px; height: 4px;
    background: var(--card-background-color); opacity: 0.6; }
  /* labels sit at their true fraction of the 0..1.1 scale, not spread evenly —
     a "1,0" pinned to the right edge would mislabel the 1.1 end */
  .nc-scale-ax { position: relative; height: 12px; font-size: 9.5px;
    color: var(--secondary-text-color); margin-top: 2px; }
  .nc-scale-ax span { position: absolute; transform: translateX(-50%); }
  .nc-scale-ax span:first-child { transform: none; }
  .nc-wsub { font-size: 11.5px; color: var(--secondary-text-color); margin: 8px 0 2px; }
  .nc-wsub b { color: var(--primary-text-color); font-variant-numeric: tabular-nums; }
  .nc-ax { font-size: 9.5px; color: var(--secondary-text-color); text-align: center; margin-top: -4px; }
  .nc-stats { font-size: 11px; color: var(--secondary-text-color); margin-top: 6px; }
  .kv-note { font-size: 11px; padding: 5px 8px; border-radius: 6px;
    background: var(--pvs-chip-bg); color: var(--secondary-text-color); margin-top: 8px; }
  .kv-note.dim { color: var(--secondary-text-color); }
`;

/* ===================== SECTION: CARD:SENSORCHECK ========================= */

// The plant's own irradiance sensor graded against an independent reanalysis
// archive, folded by sun elevation (PV Strings >= 1.26). A cheap station does
// not measure irradiance at all: it measures illuminance with an eye-weighted
// diode and divides by a constant that holds for one reference case, so the
// error grows as the sun reddens. Diagnosis only — the integration corrects
// none of it, because the nowcast, the bias layer and the sky map have
// absorbed parts of the same error already. The card says that out loud.
//
// Absence is silence here, against the usual rule: a plant with no irradiance
// sensor has no check to fail, and a card reading "0.0" would be a claim
// about a sensor that does not exist.

// Mirrors core/irradiance_check.py — quoted in the tooltip, never computed
// from: `usable` is the only truth about whether a band says anything.
const SC_BAND_MIN_KWH = 1;
const SC_BAND_MIN_DAYS = 5;

const SC_PLAIN_READINGS = {
  "not enough evidence yet": "sc_read_thin",
  "differs east to west -- check that the sensor is level": "sc_read_tilt",
  "one elevation band only -- no shape yet": "sc_read_oneband",
  "uneven across the sky, with no clear trend": "sc_read_uneven",
  "agrees with the reference": "sc_read_agrees",
  "close to the reference, but not evenly so": "sc_read_close",
};
// The compound verdicts are a direction plus a shape, so they translate as
// two pieces — German puts them in the same order, and a template keeps the
// door open where it would not.
const SC_SHAPES = [
  [", and more so as the sun drops", "sc_shape_drops"],
  [", and more so as the sun rises -- unusual", "sc_shape_rises"],
  [" by about the same amount at every sun height", "sc_shape_flat"],
];
const SC_DIRECTIONS = {
  "reads low": "sc_dir_low",
  "reads high": "sc_dir_high",
  "crosses the reference": "sc_dir_crosses",
};

// A phrase this dash does not know (newer integration) falls back to the
// English original: still true, only untranslated — better than a blank line
// where the verdict belongs.
function scReading(hass, reading) {
  if (!reading) return null;
  const plain = SC_PLAIN_READINGS[reading];
  if (plain) return t(hass, plain);
  for (const [suffix, key] of SC_SHAPES) {
    if (!reading.endsWith(suffix)) continue;
    const dir = SC_DIRECTIONS[reading.slice(0, -suffix.length)];
    if (dir) return t(hass, key, { dir: t(hass, dir) });
  }
  return reading;
}

class PvsSensorCheckCard extends PvsBaseCard {
  getCardSize() { return 4; }
  getGridOptions() { return { columns: "full", rows: "auto" }; }

  _render() {
    const hass = this._hass, cfg = this._config;
    if (!hass || !cfg) return;
    const card = (inner) => {
      this.style.removeProperty("display");
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${SC_CSS}</style><ha-card>${inner}<div class="pvs-tip"></div></ha-card>`;
      this._wireMoreInfo();
      if (!this._wired) {
        this._wired = true;
        wireTooltip(this, { selector: "[data-band]", content: (el) => this._bandTip(el) });
      }
    };
    // An unconfigured card is a mistake and says so. Everything below is a
    // legitimate absence and disappears without a word.
    if (!cfg.entity) return card(problemHTML(hass, { reason: t(hass, "no_entity_config") }));
    const hide = () => { this.shadowRoot.innerHTML = ""; this.style.display = "none"; };
    const st = hass.states[cfg.entity];
    if (!st) return hide();
    const a = st.attributes;
    if (!FEATURES.sensor_check.test(a)) return hide();
    // No irradiance sensor configured: the check does not exist for this
    // plant. Its zeros are not a reading.
    if (!a.ghi_entity && !a.illuminance_entity) return hide();

    const sc = a.sensor_check;
    const bands = sc.by_elevation ?? [];
    const usable = bands.filter((b) => b.usable && b.ratio != null);

    // ---- head --------------------------------------------------------------
    const chips = [];
    if (sc.ratio != null) {
      chips.push(`<span class="pvs-chip">${t(hass, "sc_overall")}
        <span class="v">${fmtNum(hass, sc.ratio, 2)}</span></span>`);
    }
    if (sc.slope != null) {
      chips.push(`<span class="pvs-chip" title="${esc(t(hass, "sc_slope_tip"))}">${t(hass, "sc_slope")}
        <span class="v">${fmtNum(hass, sc.slope, 2)}</span></span>`);
    }
    const tilt = sc.east_west?.difference;
    if (tilt != null) {
      // The integration's own threshold: below it the verdict stays silent
      // about tilt, so the chip must not shout either.
      const off = Math.abs(tilt) >= 0.08;
      chips.push(`<span class="pvs-chip${off ? " warn" : ""}" title="${esc(t(hass, "sc_tilt_tip"))}">${t(hass, "sc_tilt")}
        <span class="v">${fmtSigned(hass, tilt, 2)}</span></span>`);
    }
    const head = `<div class="pvs-head">
      <span class="pvs-title clickable" data-more-info="${cfg.entity}">${esc(cfg.title ?? t(hass, "sc_title"))}</span>
      ${chips.join("")}${helpChip("sensorcheck")}</div>`;

    // ---- verdict, translated ------------------------------------------------
    const verdict = scReading(hass, sc.reading);
    // Why there is no ratio yet: banked hours waiting for the archive is a
    // different state from a plant with no history at all, and only the
    // first one has an action attached to it.
    const pending = sc.hours_awaiting_reference ?? 0;
    const noRatio = sc.ratio == null
      ? withheldHTML(pending > 0 ? t(hass, "sc_never_referenced") : t(hass, "sc_no_evidence"))
      : "";

    // ---- the five bands over the sun's height -------------------------------
    // Deliberately a small viewBox: the card sits in a single column of the
    // Nerd view, and a 540-wide plot scaled down to 370 px leaves the axis
    // labels at six pixels.
    const PAD_L = 30, PAD_R = 12, PAD_T = 16, PAD_B = 30, PW = 380, PH = 128;
    const W = PAD_L + PW + PAD_R, H = PAD_T + PH + PAD_B;
    const top = Math.max(1.2, Math.ceil((Math.max(1, ...usable.map((b) => b.ratio)) + 0.12) * 10) / 10);
    const yOf = (v) => PAD_T + PH - (Math.max(0, Math.min(top, v)) / top) * PH;
    const slot = PW / Math.max(1, bands.length);
    const barW = Math.min(58, slot * 0.5);
    // A band without a verdict gets the stub the daily card gives a day
    // whose forecast was never issued: hatched, at the baseline, the height
    // of nothing. Never omitted, and never up at the bright end of the
    // scale where it would read as a value.
    const STUB = 13;

    let grid = "";
    for (const v of [0, 0.5, 1]) {
      grid += `<line class="grid" x1="${PAD_L}" y1="${yOf(v).toFixed(1)}" x2="${W - PAD_R}" y2="${yOf(v).toFixed(1)}"/>
        <text class="axis" x="${PAD_L - 5}" y="${(yOf(v) + 3).toFixed(1)}" text-anchor="end">${fmtNum(hass, v, 1)}</text>`;
    }
    // 1.0 is not a gridline, it is the thing everything is measured against.
    const refY = yOf(1).toFixed(1);
    grid += `<line class="sc-ref" x1="${PAD_L}" y1="${refY}" x2="${W - PAD_R}" y2="${refY}"/>
      <text class="sc-reflbl" x="${W - PAD_R}" y="${(yOf(1) - 5).toFixed(1)}" text-anchor="end">${t(hass, "sc_reference")}</text>`;

    let cols = "";
    bands.forEach((b, i) => {
      const cx = PAD_L + slot * (i + 0.5);
      const x = cx - barW / 2;
      const label = String(b.elevation ?? "?").replace("-", "–");
      const data = `data-band="${esc(label)}" data-usable="${b.usable ? 1 : 0}"
        data-ratio="${b.ratio ?? ""}" data-hours="${b.hours ?? 0}"
        data-days="${b.days ?? 0}" data-kwh="${b.reference_kwh ?? 0}"`;
      if (b.usable && b.ratio != null) {
        const y = yOf(b.ratio);
        cols += `<rect class="sc-bar" ${data} x="${x.toFixed(1)}" y="${y.toFixed(1)}"
            width="${barW.toFixed(1)}" height="${Math.max(1, PAD_T + PH - y).toFixed(1)}" rx="2"/>
          <text class="sc-val" x="${cx.toFixed(1)}" y="${(y - 5).toFixed(1)}" text-anchor="middle">${fmtNum(hass, b.ratio, 2)}</text>`;
      } else {
        const y = PAD_T + PH - STUB;
        cols += `<rect class="sc-stub" ${data} x="${x.toFixed(1)}" y="${y.toFixed(1)}"
            width="${barW.toFixed(1)}" height="${STUB}" rx="2" fill="var(--pvs-unobserved)"/>
          <rect class="sc-stub" ${data} x="${x.toFixed(1)}" y="${y.toFixed(1)}"
            width="${barW.toFixed(1)}" height="${STUB}" rx="2" fill="url(#pvs-sc-hatch)"/>
          <text class="sc-notyet" x="${cx.toFixed(1)}" y="${(y - 5).toFixed(1)}" text-anchor="middle">${t(hass, "sc_band_unusable")}</text>`;
      }
      cols += `<text class="axis" x="${cx.toFixed(1)}" y="${(H - PAD_B + 14).toFixed(1)}" text-anchor="middle">${esc(label)}</text>`;
    });
    cols += `<text class="sc-axtitle" x="${(PAD_L + PW / 2).toFixed(1)}" y="${H - 4}" text-anchor="middle">${t(hass, "sc_axis_elev")} (°)</text>`;

    // ---- evidence, small, underneath ----------------------------------------
    // Evidence and its provenance are one thought and share a line: the card
    // sits in a single column of the Nerd view, where every line costs height.
    const ev = [];
    const counts = [];
    if (sc.hours) {
      counts.push(t(hass, "sc_evidence", {
        hours: fmtNum(hass, sc.hours, 0), days: fmtNum(hass, sc.days ?? 0, 0),
        kwh: fmtNum(hass, sc.reference_kwh ?? 0, 0),
      }));
    }
    if (sc.reference_sources?.length) {
      counts.push(t(hass, "sc_sources", { sources: sc.reference_sources.join(", ") }));
    }
    if (counts.length) ev.push(counts.join(" · "));
    // Only once there is a ratio: before that the withheld line above has
    // already said what is missing, and repeating the count reads as two
    // different problems.
    if (pending > 0 && sc.ratio != null) ev.push(t(hass, "sc_pending", { n: fmtNum(hass, pending, 0) }));

    card(`
      ${head}
      ${verdict ? `<div class="sc-verdict">${esc(verdict)}</div>` : ""}
      ${noRatio}
      <div class="sc-wrap"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="aspect-ratio:${W}/${H}">
        <defs>${hatchPattern("pvs-sc-hatch")}</defs>
        ${grid}${cols}
      </svg></div>
      <div class="pvs-legend">
        <span class="it"><span class="sw" style="background:var(--pvs-measure)"></span>${t(hass, "sc_measured")}</span>
        <span class="it"><svg width="16" height="10"><line x1="0" y1="5" x2="16" y2="5"
          stroke="var(--primary-text-color)" stroke-width="1.4" stroke-dasharray="5 3" opacity="0.55"/></svg>${t(hass, "sc_reference")}</span>
        <span class="it"><svg width="14" height="10"><rect width="14" height="10" rx="2" fill="var(--pvs-unobserved)"/><rect width="14" height="10" rx="2" fill="url(#pvs-sc-hatch-l)"/><defs>${hatchPattern("pvs-sc-hatch-l")}</defs></svg>${t(hass, "sc_band_unusable")}</span>
      </div>
      ${ev.map((line) => `<div class="sc-ev">${line}</div>`).join("")}
      <div class="sc-note">${t(hass, "sc_note")}</div>`);
  }

  _bandTip(el) {
    const hass = this._hass, d = el.dataset;
    const row = (k, v) => `<div class="r"><span class="k">${k}</span><span class="v">${v}</span></div>`;
    return `<div class="h">${esc(d.band)}°</div>
      ${d.ratio ? row(t(hass, "sc_tip_ratio"), fmtNum(hass, Number(d.ratio), 3)) : ""}
      ${row(t(hass, "sc_tip_hours"), fmtNum(hass, Number(d.hours), 0))}
      ${row(t(hass, "sc_tip_days"), fmtNum(hass, Number(d.days), 0))}
      ${row(t(hass, "sc_tip_ref"), `${fmtNum(hass, Number(d.kwh), 1)} kWh/m²`)}
      ${d.usable === "0" ? `<div class="k" style="margin-top:5px">${t(hass, "sc_band_why", { kwh: SC_BAND_MIN_KWH, days: SC_BAND_MIN_DAYS })}</div>` : ""}`;
  }

  static getConfigElement() { return document.createElement("pvstrings-chain-editor"); }
  static getStubConfig() { return { entity: "" }; }
}

const SC_CSS = `
  .sc-verdict { font-size: 17px; font-weight: 600; line-height: 1.35;
    color: var(--primary-text-color); margin: 2px 0 10px; }
  .sc-wrap { margin-top: 4px; }
  .sc-wrap svg { width: 100%; height: auto; display: block; }
  .sc-bar { fill: var(--pvs-measure); cursor: help; }
  .sc-stub { cursor: help; }
  .sc-bar:hover { opacity: 0.85; }
  .sc-val { fill: var(--primary-text-color); font-size: 10.5px;
    font-family: var(--pvs-mono); font-variant-numeric: tabular-nums; }
  .sc-notyet { fill: var(--secondary-text-color); font-size: 9.5px; }
  .sc-ref { stroke: var(--primary-text-color); stroke-width: 1.4;
    stroke-dasharray: 5 3; opacity: 0.55; }
  .sc-reflbl, .sc-axtitle { fill: var(--secondary-text-color); font-size: 9.5px; }
  .sc-ev { font-size: 11px; color: var(--secondary-text-color); margin-top: 4px; line-height: 1.5; }
  .sc-note { font-size: 11px; line-height: 1.5; padding: 6px 9px; border-radius: 6px;
    background: var(--pvs-chip-bg); color: var(--secondary-text-color); margin-top: 10px; }
`;

/* ===================== SECTION: CARD:CALIBRATION ========================= */

// The second model branch, running beside the published one.
//
// The sensor reads low by an amount that depends on the sun's height, and
// every layer this plant has learned was trained while reading it. A factor
// in front of the sensor would correct the same error twice, so instead a
// whole second branch reads it through a correction curve and learns its own
// model from scratch. Both are scored on the same clear hours; a criterion
// written down before the trial began decides which one gets published.
//
// The hard part is the first six weeks, when there is nothing to draw but
// the progress. Design rule 1 carries the whole card here: "not yet" and
// "nothing" must never look the same, and neither may look like "broken".
// So the axes are drawn empty rather than hidden, the day count is the
// headline while it is the only real figure, and the blocking reasons that
// are mere consequences of having no days are not listed at all.

const CAL_TTL = 30 * 60000;

// Read-only and slow-moving: a clear day arrives once a day at most.
function calibrationTrial(hass, entryId) {
  return cachedWS(`cal-trial|${entryId}`, CAL_TTL, async () => {
    const r = await callPvsService(hass, "calibration_trial", { config_entry_id: entryId });
    if (!r || typeof r !== "object" || r.day_ahead == null) {
      throw new Error("calibration_trial: unexpected response");
    }
    return r;
  });
}

// The integration writes its blocking reasons in English, from a closed set.
// Translated here rather than passed through; an unknown phrasing falls back
// to the original, which is still true — the same bargain the sensor check
// makes.
const CAL_BLOCK_PLAIN = {
  "no profile on one of the branches": "cal_block_noprofile",
  "no level on one of the branches": "cal_block_nolevel",
};
const CAL_BLOCK_PATTERNS = [
  [/^only (\d+) clear days, needs (\d+)$/, "cal_block_days", ["n", "m"]],
  [/^worst hour ([\d.]+) against ([\d.]+), needs ([\d.]+)$/, "cal_block_worst", ["a", "b", "c"]],
  [/^level ([\d.]+) drifted further than ([\d.]+)$/, "cal_block_drift", ["a", "b"]],
];
function calBlocking(hass, reason) {
  if (!reason) return null;
  const plain = CAL_BLOCK_PLAIN[reason];
  if (plain) return t(hass, plain);
  for (const [re, key, names] of CAL_BLOCK_PATTERNS) {
    const m = reason.match(re);
    if (!m) continue;
    const vars = {};
    // Localised, but with the source's own precision: rounding "1.002" to
    // two places would turn a drift verdict into "1,00 against 1,00".
    names.forEach((name, i) => {
      const raw = m[i + 1];
      vars[name] = fmtNum(hass, Number(raw), (raw.split(".")[1] ?? "").length);
    });
    return t(hass, key, vars);
  }
  return reason;
}

// The single sensor-check verdict that settles it: this instrument is
// straight, and no correction curve would improve on it. Every other reading
// leaves the question open, and the card says so rather than guessing.
const CAL_AGREES = (sc) => sc?.reading === "agrees with the reference";

class PvsCalibrationCard extends PvsBaseCard {
  getCardSize() { return 5; }
  getGridOptions() { return { columns: "full", rows: "auto" }; }
  setConfig(config) {
    this._state = null;
    this._horizon = config?.horizon === "now" ? "now" : "da";
    super.setConfig(config);
  }
  // Service data, not state data: re-read when the cache has run out.
  _shouldUpdate(prev) {
    return !prev || (!!this._state?.at && Date.now() - this._state.at > CAL_TTL);
  }

  async _load() {
    const hass = this._hass, cfg = this._config;
    const had = this._state?.data ? this._state : null;
    this._state = had ? { ...had, at: Date.now() } : { loading: true, at: Date.now() };
    try {
      const m = await getRegistryModel(hass);
      const info = m.byEntityId.get(cfg.entity);
      const node = info?.level === "plant" ? info.node : info?.node?.plant;
      if (!node?.entryId) throw new Error(t(hass, "entity_missing", { entity: cfg.entity }));
      this._state = { at: Date.now(), data: await calibrationTrial(hass, node.entryId) };
    } catch (e) {
      // A failed read keeps the last good answer rather than blanking the
      // card: this one is watched for weeks, and an empty card on a hiccup
      // is exactly the "something is broken" it must never say.
      this._state = had ?? { at: Date.now(), problem: e?.message ?? String(e) };
    }
    this._render();
  }

  _render() {
    const hass = this._hass, cfg = this._config;
    if (!hass || !cfg) return;
    const card = (inner) => {
      this.style.removeProperty("display");
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${CAL_CSS}</style><ha-card>${inner}<div class="pvs-tip"></div></ha-card>`;
      this._wire();
    };
    if (!cfg.entity) return card(problemHTML(hass, { reason: t(hass, "no_entity_config") }));
    const hide = () => { this.shadowRoot.innerHTML = ""; this.style.display = "none"; };
    const st = hass.states[cfg.entity];
    if (!st) return hide();
    // Absence is silence, as on the sensor check: an integration without the
    // service, and a plant with no irradiance sensor, have no trial to show.
    if (!hasPvsService(hass, "calibration_trial")) return hide();
    const a = st.attributes;
    if (!a.ghi_entity && !a.illuminance_entity) return hide();

    if (!this._state || (this._state.at && Date.now() - this._state.at > CAL_TTL)) this._load();

    const help = helpChip("calibration");
    const head = (chip = "") => `<div class="pvs-head">
      <span class="pvs-title clickable" data-more-info="${cfg.entity}">${esc(cfg.title ?? t(hass, "cal_title"))}</span>
      ${chip}${help}</div>`;

    if (!this._state || this._state.loading) {
      return card(head() + `<div class="pvs-sub">${t(hass, "cal_loading")}</div>`);
    }
    if (this._state.problem) {
      return card(head() + problemHTML(hass, { reason: esc(this._state.problem), entity: cfg.entity }));
    }
    const d = this._state.data;

    // ---- no branch running ------------------------------------------------
    // "not running" covers three different situations and they must not read
    // alike. An integration from before the option only ever meant the last
    // of them, so that is what a missing `reason` falls back to.
    if (!d.running) {
      const reason = d.reason ?? "not_enough_evidence";
      // Never reached in practice: the entity guard above already withdrew
      // where there is no instrument. It is here because the integration
      // tests the option BEFORE the instrument, so a sensorless plant with
      // the trial off reports `not_enabled` — which is exactly why that
      // guard reads the entity and not this field.
      if (reason === "no_sensor") return hide();
      // The interesting one: the owner can see a skewed sensor and do
      // something about it. An offer, not a warning — and it names where the
      // switch is rather than leaving that to be searched for.
      if (reason === "not_enabled") {
        const sc = a.sensor_check;
        const finding = sc?.ratio != null ? scReading(hass, sc.reading) : null;
        // A sensor the check calls straight needs no trial, so it is not
        // offered one — the finding stands on its own.
        const offer = finding && !CAL_AGREES(sc);
        return card(head(`<span class="pvs-chip">${t(hass, "cal_off")}</span>`) + (offer
          ? `<div class="cal-finding">${esc(finding)}</div>
             <div class="cal-chips">
               <span class="pvs-chip">${t(hass, "sc_overall")} <span class="v">${fmtNum(hass, sc.ratio, 2)}</span></span>
               ${sc.slope != null ? `<span class="pvs-chip">${t(hass, "sc_slope")}
                 <span class="v">${fmtNum(hass, sc.slope, 2)}</span></span>` : ""}
             </div>
             <div class="cal-note">${t(hass, "cal_offer")}</div>
             <div class="cal-where">${t(hass, "cal_where")}</div>`
          // Nothing to offer a trial for: no finding at all, or one that
          // says the sensor is straight. Suggesting it here would be asking
          // for a second forecast against an error nobody has measured.
          : `${finding ? `<div class="cal-finding">${esc(finding)}</div>` : ""}
             <div class="cal-note">${t(hass, finding ? "cal_note_idle" : "cal_off_nofinding")}</div>`));
      }
      // Switched on, no branch yet: either the check has not gathered enough
      // to build a curve from, or what it has justifies no correction. The
      // published check carries no band's trust, so the two cannot be told
      // apart here — and only one of them can be stated outright, which is a
      // check that calls the sensor straight. Neither is a fault.
      const agrees = CAL_AGREES(a.sensor_check);
      return card(
        head(`<span class="pvs-chip">${t(hass, agrees ? "cal_not_needed" : "cal_no_evidence")}</span>`)
        + `<div class="cal-note">${t(hass, agrees ? "cal_note_idle" : "cal_note_wait")}</div>`);
    }

    // ---- running ----------------------------------------------------------
    const h = this._horizon === "now" ? d.short_term : d.day_ahead;
    const v = h?.verdict ?? {};
    const days = v.days ?? h?.days ?? 0;
    const needed = v.days_needed ?? 0;

    const pct = needed > 0 ? Math.min(100, (days / needed) * 100) : 0;
    const archived = h?.hours_archived ?? 0;
    const progress = `
      <div class="cal-prog">
        <div class="cal-prog-row">
          <span class="cal-prog-lbl">${t(hass, "cal_days", {
            days: `<b class="pvs-num">${fmtNum(hass, days, 0)}</b>`,
            needed: `<span class="pvs-num">${fmtNum(hass, needed, 0)}</span>` })}</span>
        </div>
        <div class="cal-track"><div class="cal-fill" style="width:${Math.max(1.5, pct).toFixed(1)}%"></div></div>
        <div class="pvs-sub">${archived > 0
          ? t(hass, "cal_hours_sub", { archived: fmtNum(hass, archived, 0), clear: fmtNum(hass, h?.hours_clear ?? 0, 0) })
          : t(hass, "cal_hours_none")}</div>
      </div>`;

    // The question with its own line, because it is the one people actually
    // came to ask. Read off the response, never inferred from the verdict.
    const applied = d.applied === true || d.published_branch === "calibrated";
    const published = `<div class="cal-pub${applied ? " on" : ""}">${
      t(hass, applied ? "cal_published_cal" : "cal_published_live")}</div>`;

    // What is being tried, in one line of chips.
    const c = d.curve;
    const first = c?.knots?.[0], last = c?.knots?.[c.knots.length - 1];
    const curveChips = c ? `<div class="cal-chips">
      <span class="pvs-chip"><span class="cal-sw cal"></span>${t(hass, "cal_curve")}</span>
      ${first && last ? `<span class="pvs-chip"><span class="v">${t(hass, "cal_curve_range", {
        from: fmtNum(hass, first.factor, 2), low: fmtNum(hass, first.elevation, 0),
        to: fmtNum(hass, last.factor, 2), high: fmtNum(hass, last.elevation, 0) })}</span></span>` : ""}
      ${c.capped ? `<span class="pvs-chip warn">${t(hass, "cal_curve_capped")}</span>` : ""}
      ${c.revision ? `<span class="pvs-chip dim">${t(hass, "cal_curve_rev", { rev: esc(String(c.revision).slice(0, 8)) })}</span>` : ""}
    </div>` : "";

    const tab = (key, label) => `<span class="pvs-chip cal-tab${this._horizon === key ? " on" : ""}"
      data-horizon="${key}" title="${esc(t(hass, "cal_h_tip"))}">${label}</span>`;
    const tabs = `<div class="cal-tabs">${tab("da", t(hass, "cal_h_da"))}${tab("now", t(hass, "cal_h_now"))}</div>`;

    // ---- the two hourly profiles against 1.0 ------------------------------
    const hours = Array.isArray(h?.hours_of_day) ? h.hours_of_day.slice().sort((x, y) => x - y) : [];
    const live = h?.live_profile ?? {}, shadow = h?.shadow_profile ?? {};
    const vals = hours.flatMap((x) => [live[String(x)], shadow[String(x)]])
      .filter((x) => typeof x === "number" && Number.isFinite(x));
    // An empty trial still gets a chart: axes, the reference it will be read
    // against, and a sentence in the middle saying what is missing. Hiding it
    // would make the card look like it had failed to draw.
    const HOURS_EMPTY = [5, 7, 9, 11, 13, 15, 17, 19];
    const xs = hours.length ? hours : HOURS_EMPTY;
    const lo = Math.min(0.8, ...vals.map((x) => x - 0.05));
    const hi = Math.max(1.2, ...vals.map((x) => x + 0.05));
    const PAD_L = 32, PAD_R = 12, PAD_T = 14, PAD_B = 28, PW = 380, PH = 104;
    const W = PAD_L + PW + PAD_R, H = PAD_T + PH + PAD_B;
    const x0 = xs[0], x1 = xs[xs.length - 1];
    const xOf = (x) => PAD_L + (x1 === x0 ? PW / 2 : ((x - x0) / (x1 - x0)) * PW);
    const yOf = (y) => PAD_T + PH - ((Math.max(lo, Math.min(hi, y)) - lo) / (hi - lo)) * PH;

    let grid = "";
    for (const g of [lo, 1, hi]) {
      grid += `<line class="grid" x1="${PAD_L}" y1="${yOf(g).toFixed(1)}" x2="${W - PAD_R}" y2="${yOf(g).toFixed(1)}"/>
        <text class="axis" x="${PAD_L - 5}" y="${(yOf(g) + 3).toFixed(1)}" text-anchor="end">${fmtNum(hass, g, 2)}</text>`;
    }
    grid += `<line class="cal-ref" x1="${PAD_L}" y1="${yOf(1).toFixed(1)}" x2="${W - PAD_R}" y2="${yOf(1).toFixed(1)}"/>`;
    for (const x of xs.filter((_, i) => i % Math.ceil(xs.length / 8) === 0)) {
      grid += `<text class="axis" x="${xOf(x).toFixed(1)}" y="${(H - PAD_B + 13).toFixed(1)}" text-anchor="middle">${x}</text>`;
    }
    grid += `<text class="cal-axtitle" x="${(PAD_L + PW / 2).toFixed(1)}" y="${H - 3}" text-anchor="middle">${t(hass, "cal_axis_hour")}</text>`;

    const lineOf = (prof, cls) => {
      const pts = hours.map((x) => [x, prof[String(x)]])
        .filter(([, y]) => typeof y === "number" && Number.isFinite(y));
      if (!pts.length) return "";
      const dd = pts.map(([x, y], i) => `${i ? "L" : "M"}${xOf(x).toFixed(1)} ${yOf(y).toFixed(1)}`).join("");
      const dots = pts.map(([x, y]) => `<circle class="${cls}" cx="${xOf(x).toFixed(1)}" cy="${yOf(y).toFixed(1)}" r="2.4"/>`).join("");
      return `<path class="${cls}" d="${dd}" fill="none"/>${dots}`;
    };
    const empty = !hours.length;
    const plot = empty
      ? `<text class="cal-empty" x="${(PAD_L + PW / 2).toFixed(1)}" y="${(PAD_T + PH * 0.3).toFixed(1)}" text-anchor="middle">${t(hass, "cal_chart_empty")}</text>
         <text class="cal-empty-sub" x="${(PAD_L + PW / 2).toFixed(1)}" y="${(PAD_T + PH * 0.3 + 13).toFixed(1)}" text-anchor="middle">${t(hass, "cal_chart_empty_sub")}</text>`
      : lineOf(live, "cal-l-live") + lineOf(shadow, "cal-l-cal");

    // ---- the two number pairs ---------------------------------------------
    const cell = (value, digits, better) => value == null
      ? `<td class="cal-ny">${t(hass, "cal_not_yet")}</td>`
      : `<td class="pvs-num${better ? " good" : ""}">${fmtNum(hass, value, digits)}</td>`;
    const lw = h?.live_worst_hour, sw = h?.shadow_worst_hour;
    const ll = h?.live_level, sl = h?.shadow_level;
    const numbers = `<table class="cal-num">
      <tr><th></th><th><span class="cal-sw live"></span>${t(hass, "cal_col_live")}</th>
        <th><span class="cal-sw cal"></span>${t(hass, "cal_col_cal")}</th></tr>
      <tr><th class="cal-help" title="${esc(t(hass, "cal_row_worst_tip"))}">${t(hass, "cal_row_worst")}</th>
        ${cell(lw, 2, false)}${cell(sw, 2, lw != null && sw != null && sw < lw)}</tr>
      <tr><th class="cal-help" title="${esc(t(hass, "cal_row_level_tip"))}">${t(hass, "cal_row_level")}</th>
        ${cell(ll, 3, false)}${cell(sl, 3, ll != null && sl != null && Math.abs(sl - 1) < Math.abs(ll - 1))}</tr>
    </table>`;

    // How much of the worst hour's error has gone, against how much has to.
    // Same grammar as the maturity bar: a tick where the target sits.
    const imp = h?.improvement, minImp = v.min_improvement ?? 0.5;
    const impBar = imp == null ? "" : `
      <div class="cal-imp">
        <div class="cal-prog-row"><span class="cal-prog-lbl">${t(hass, "cal_improvement")}
          <b class="pvs-num${imp >= minImp ? " good" : ""}">${fmtPct(hass, imp * 100, 0)}</b></span>
          <span class="pvs-sub">${t(hass, "cal_improvement_needed", { pct: fmtPct(hass, minImp * 100, 0) })}</span></div>
        <div class="cal-track">
          <div class="cal-fill${imp >= minImp ? " good" : ""}" style="width:${Math.max(0, Math.min(100, imp * 100)).toFixed(1)}%"></div>
          <div class="cal-tick" style="left:${(minImp * 100).toFixed(1)}%"></div>
        </div>
      </div>`;

    // ---- the verdict ------------------------------------------------------
    // While the day count is short, every other blocking reason is a
    // consequence of it. Listing "no profile" and "no level" underneath would
    // read as three faults where there is one clock still running.
    let verdict;
    if (v.decided) {
      verdict = `<div class="cal-verdict good">${t(hass, "cal_decided")}</div>
        <div class="pvs-sub">${t(hass, "cal_decided_note")}</div>`;
    } else if (days < needed) {
      verdict = `<div class="cal-verdict">${t(hass, "cal_waiting", {
        needed: fmtNum(hass, needed, 0),
        improvement: fmtPct(hass, minImp * 100, 0),
        drift: fmtNum(hass, v.max_level_drift ?? 0.02, 2) })}</div>`;
    } else {
      const reasons = (v.blocking ?? []).map((r) => calBlocking(hass, r)).filter(Boolean);
      verdict = `<div class="cal-verdict">${t(hass, "cal_blocking_head")}</div>
        <ul class="cal-block">${reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>`;
    }

    card(`
      ${head(`<span class="pvs-chip"><span class="cal-dot"></span>${t(hass, "cal_running")}</span>`)}
      ${progress}
      ${published}
      ${curveChips}
      ${tabs}
      <div class="cal-wrap"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="aspect-ratio:${W}/${H}">
        ${grid}${plot}
      </svg></div>
      <div class="pvs-legend">
        <span class="it"><span class="sw" style="background:var(--pvs-model)"></span>${t(hass, "cal_leg_live")}</span>
        <span class="it"><span class="sw" style="background:var(--pvs-cat-3)"></span>${t(hass, "cal_leg_cal")}</span>
        <span class="it"><svg width="16" height="10"><line x1="0" y1="5" x2="16" y2="5"
          stroke="var(--primary-text-color)" stroke-width="1.4" stroke-dasharray="5 3" opacity="0.55"/></svg>${t(hass, "cal_leg_ref")}</span>
      </div>
      ${numbers}
      ${impBar}
      ${verdict}
      <div class="cal-note">${t(hass, "cal_note")}</div>`);
  }

  _wire() {
    this._wireMoreInfo();
    if (!this._tabsWired) {
      this._tabsWired = true;
      this.shadowRoot.addEventListener("click", (ev) => {
        const el = ev.target.closest?.("[data-horizon]");
        if (!el) return;
        this._horizon = el.getAttribute("data-horizon");
        this._render();
      });
    }
    if (!this._wired) { this._wired = true; wireTooltip(this); }
  }

  static getConfigElement() { return document.createElement("pvstrings-chain-editor"); }
  static getStubConfig() { return { entity: "" }; }
}

const CAL_CSS = `
  .cal-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block;
    background: var(--pvs-cat-3);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--pvs-cat-3) 25%, transparent); }
  .cal-prog { margin: 4px 0 12px; }
  .cal-prog .pvs-sub { margin-top: 6px; display: block; }
  .cal-prog-row { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-bottom: 5px; }
  .cal-prog-lbl { font-size: 12.5px; color: var(--secondary-text-color); }
  .cal-prog-lbl b { font-size: 15px; font-weight: 600; color: var(--primary-text-color); }
  .cal-prog-lbl b.good { color: var(--success-color, #43a047); }
  .cal-track { position: relative; height: 6px; border-radius: 3px; background: var(--pvs-unobserved); }
  .cal-fill { height: 100%; border-radius: 3px; background: var(--pvs-cat-3); }
  .cal-fill.good { background: var(--success-color, #43a047); }
  .cal-tick { position: absolute; top: -3px; bottom: -3px; width: 2px; border-radius: 1px;
    background: color-mix(in srgb, var(--primary-text-color, #212121) 40%, transparent); }
  /* the answer to "is it already steering my plant" — its own block, never a tooltip */
  .cal-pub { font-size: 13px; font-weight: 500; line-height: 1.4; color: var(--primary-text-color);
    background: var(--pvs-chip-bg); border-left: 3px solid var(--pvs-model);
    border-radius: 6px; padding: 8px 11px; margin: 0 0 10px; }
  .cal-pub.on { border-left-color: var(--pvs-cat-3); }
  .cal-chips { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
  .cal-finding { font-size: 15px; font-weight: 600; line-height: 1.35;
    color: var(--primary-text-color); margin: 2px 0 9px; }
  .cal-where { font-size: 11px; line-height: 1.5; margin-top: 6px; padding: 6px 9px;
    border-radius: 6px; color: var(--primary-text-color);
    background: var(--pvs-chip-bg); border-left: 3px solid var(--pvs-model); }
  .pvs-chip.dim { opacity: 0.7; }
  .cal-tabs { display: flex; gap: 6px; margin-bottom: 6px; }
  .cal-tab { cursor: pointer; border-color: var(--pvs-hairline); }
  .cal-tab.on { color: var(--primary-text-color); border-color: var(--pvs-model);
    background: color-mix(in srgb, var(--pvs-model) 14%, transparent); }
  .cal-wrap svg { width: 100%; height: auto; display: block; }
  .cal-ref { stroke: var(--primary-text-color); stroke-width: 1.4; stroke-dasharray: 5 3; opacity: 0.55; }
  .cal-axtitle { fill: var(--secondary-text-color); font-size: 9.5px; }
  .cal-empty { fill: var(--primary-text-color); font-size: 12px; }
  .cal-empty-sub { fill: var(--secondary-text-color); font-size: 9.5px; }
  path.cal-l-live { stroke: var(--pvs-model); stroke-width: 2.2; stroke-linejoin: round; }
  circle.cal-l-live { fill: var(--pvs-model); }
  path.cal-l-cal { stroke: var(--pvs-cat-3); stroke-width: 2.2; stroke-linejoin: round; }
  circle.cal-l-cal { fill: var(--pvs-cat-3); }
  .cal-sw { display: inline-block; width: 9px; height: 9px; border-radius: 2px;
    margin-right: 5px; vertical-align: middle; }
  .cal-sw.live { background: var(--pvs-model); }
  .cal-sw.cal { background: var(--pvs-cat-3); }
  table.cal-num { border-collapse: collapse; width: 100%; font-size: 11.5px; margin-top: 8px; }
  table.cal-num th, table.cal-num td { text-align: right; padding: 4px 8px;
    border-bottom: 1px solid var(--pvs-hairline); }
  table.cal-num tr > th:first-child { text-align: left; color: var(--secondary-text-color);
    font-weight: 500; white-space: nowrap; }
  table.cal-num th.cal-help { cursor: help; }
  table.cal-num td { color: var(--primary-text-color); }
  table.cal-num td.good { color: var(--success-color, #43a047); font-weight: 600; }
  table.cal-num td.cal-ny { color: var(--secondary-text-color); font-style: italic; opacity: 0.75; }
  .cal-imp { margin-top: 10px; }
  .cal-verdict { font-size: 12.5px; line-height: 1.5; color: var(--primary-text-color); margin-top: 10px; }
  .cal-verdict.good { color: var(--success-color, #43a047); font-weight: 600; }
  ul.cal-block { margin: 4px 0 0; padding-left: 18px; font-size: 11.5px; line-height: 1.6;
    color: var(--secondary-text-color); }
  .cal-note { font-size: 11px; line-height: 1.5; padding: 6px 9px; border-radius: 6px;
    background: var(--pvs-chip-bg); color: var(--secondary-text-color); margin-top: 10px; }
`;

/* ====================== SECTION: CARD:CURVE ============================== */

// The learned conversion curve against the prior it started from.
// Three states the integration deliberately keeps apart, and so does this
// card (rule 1): learning off (nothing), learning on but no support point
// moved yet (not yet — only visible in the diagnostics), and learned.
// A point that has not moved is drawn hollow on the prior line, never as a
// measurement.
class PvsCurveCard extends PvsBaseCard {
  getCardSize() { return 4; }
  getGridOptions() { return { columns: "full", rows: "auto" }; }

  async _loadCurves() {
    if (this._curves) return;
    this._curves = "loading";
    let out = { error: "unavailable" };
    try {
      const hass = this._hass;
      const m = await getRegistryModel(hass);
      const info = m.byEntityId.get(this._config.entity);
      const node = info?.node;
      // keys are "<scope_id>|<stage>" (as in conversion_evidence); a bare
      // scope_id is accepted too — the shape is not contracted yet
      out = await conversionState(hass, node?.entryId ?? m.plants[0]?.entryId, node?.scopeId);
    } catch (_) { /* stays unavailable */ }
    this._curves = out;
    this._render();
  }

  _render() {
    const hass = this._hass, cfg = this._config;
    if (!hass || !cfg) return;
    const card = (inner) => {
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${FC_CSS}${CURVE_CSS}</style><ha-card>${inner}<div class="pvs-tip"></div></ha-card>`;
      this._wire();
    };
    if (!cfg.entity) return card(problemHTML(hass, { reason: t(hass, "no_entity_config") }));
    const st = hass.states[cfg.entity];
    if (!st) return card(problemHTML(hass, { reason: t(hass, "entity_missing", { entity: cfg.entity }) }));
    const a = st.attributes;
    const title = cfg.title ?? t(hass, "curve_title");
    const head = (extra = "") => `<div class="pvs-head">
      <span class="pvs-title clickable" data-more-info="${cfg.entity}">${esc(title)}</span>${extra}</div>`;

    // Learned state comes from the sensor; anything else needs diagnostics.
    let learning = a.conversion_learning ?? null;
    let gathering = false;
    if (!learning) {
      const d = this._curves;
      if (!d || d === "loading") {
        this._loadCurves();
        return card(head() + `<div class="kv-note dim">${t(hass, "conv_ev_loading")}</div>`);
      }
      if (d.error === "admin") return card(head() + `<div class="kv-note warn">${t(hass, "conv_ev_admin")}</div>`);
      if (d.error === "unsupported") return card(head() + withheldHTML(t(hass, "curve_unsupported")));
      if (d.error === "pending") {
        // No region built yet. Whether learning is switched on cannot be read
        // from here — so say what IS known: what the collector is doing.
        return card(head() + withheldHTML(!d.collecting ? t(hass, "curve_not_collected")
          : d.rows === 0 ? t(hass, "curve_silent")
          : t(hass, "curve_pending", { usable: d.usable, rows: d.rows })));
      }
      if (d.error) return card(head() + `<div class="kv-note warn">${t(hass, "conv_ev_unavailable")}</div>`);
      learning = d.learning;
      gathering = true;
    }

    const bins = learning?.bins ?? {};
    const pts = Object.entries(bins)
      .map(([k, v]) => ({ load: parseFloat(k), eta: v?.eta, prior: v?.prior,
        measured: v?.measured ?? null, spread: v?.spread ?? null,
        n: v?.n_eff ?? 0, learned: !!v?.learned, reachable: v?.reachable !== false }))
      .filter((p) => isFinite(p.load) && p.load > 0 && p.eta != null)
      .sort((x, y) => x.load - y.load);
    if (!pts.length) return card(head() + withheldHTML(t(hass, "curve_bins_missing")));

    const blocked = learning?.blocked ?? null;
    const maxLoad = learning?.max_load ?? null;
    const cov = learning?.coverage;
    const anyLearned = pts.some((p) => p.learned);
    const anyMeasured = pts.some((p) => p.measured != null);

    const chips = [];
    // A coverage of 0 next to a blocked note would read as failure; the note
    // already says why nothing moved.
    if (cov != null && !blocked) {
      chips.push(`<span class="pvs-chip" title="${esc(t(hass, "curve_coverage_tip"))}">
        ${t(hass, "curve_coverage")} <span class="v">${fmtNum(hass, cov * 100, 0)} %</span></span>`);
    }
    if (maxLoad != null) {
      chips.push(`<span class="pvs-chip" title="${esc(t(hass, "curve_maxload_tip"))}">
        ${t(hass, "curve_maxload")} <span class="v">${fmtNum(hass, maxLoad * 100, 0)} %</span></span>`);
    }
    if (learning?.stage) chips.push(`<span class="pvs-chip dim">${esc(stageLabel(hass, learning.stage))}</span>`);

    // ---- geometry: log-x (load decades), plus a delta strip ----------------
    // The curve spans tens of percentage points while learning moves points by
    // tenths of one. At a single scale the correction is invisible, so shape
    // lives in the main plot and correction in its own strip below.
    const PAD_L = 42, PAD_R = 28, PAD_T = 18, PW = 620, PH = 176;
    const GAP = 14, STRIP_H = anyLearned ? 42 : 0, PAD_B = 30;
    const STRIP_Y = PAD_T + PH + GAP;
    const W = PAD_L + PW + PAD_R;
    const H = STRIP_Y + STRIP_H + PAD_B;
    const loads = pts.map((p) => p.load);
    const lMin = Math.min(...loads), lMax = Math.max(...loads);
    const lgMin = Math.log10(lMin), lgMax = Math.log10(Math.max(lMax, lMin * 1.5));
    const xOf = (l) => PAD_L + ((Math.log10(l) - lgMin) / (lgMax - lgMin || 1)) * PW;
    const vals = pts.flatMap((p) => [p.eta, p.prior, p.measured].filter((v) => v != null));
    let vMin = Math.min(...vals), vMax = Math.max(...vals);
    const padV = Math.max(0.005, (vMax - vMin) * 0.12);
    vMin = Math.max(0, vMin - padV); vMax = Math.min(1, vMax + padV);
    const yOf = (v) => PAD_T + PH - ((v - vMin) / (vMax - vMin || 1)) * PH;

    let grid = "";
    for (const tick of [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1]) {
      if (tick < lMin * 0.99 || tick > lMax * 1.01) continue;
      const x = xOf(tick);
      grid += `<line class="grid" x1="${x}" y1="${PAD_T}" x2="${x}" y2="${STRIP_Y + STRIP_H}"/>
        <text class="axis" x="${x}" y="${H - 14}" text-anchor="middle">${fmtNum(hass, tick * 100, 0)} %</text>`;
    }
    for (let i = 0; i <= 2; i++) {
      const v = vMin + (i / 2) * (vMax - vMin);
      grid += `<line class="grid" x1="${PAD_L}" y1="${yOf(v)}" x2="${W - PAD_R}" y2="${yOf(v)}"/>
        <text class="axis" x="${PAD_L - 5}" y="${yOf(v) + 3}" text-anchor="end">${fmtNum(hass, v * 100, 1)}</text>`;
    }
    grid += `<text class="axis" x="${PAD_L - 5}" y="${PAD_T - 7}" text-anchor="end" style="font-size:8.5px">%</text>
      <text class="axis" x="${PAD_L + 2}" y="${PAD_T - 7}" style="font-size:8.5px">${t(hass, "curve_axis_eta")}</text>
      <text class="axis" x="${PAD_L + PW / 2}" y="${H - 3}" text-anchor="middle">${t(hass, "curve_axis_load")}</text>`;

    // Loads the plant has never reached: hatched, exactly like an unobserved
    // sky cell — same grammar for the same meaning ("never seen"), and never
    // confusable with "measured and found equal to the datasheet".
    let unreach = "";
    const firstUnreach = pts.find((p) => !p.reachable);
    if (firstUnreach) {
      const x0 = maxLoad != null ? xOf(Math.min(Math.max(maxLoad, lMin), lMax))
        : xOf(firstUnreach.load);
      unreach = `<rect x="${x0.toFixed(1)}" y="${PAD_T}" width="${(W - PAD_R - x0).toFixed(1)}"
          height="${PH}" fill="url(#pvs-curve-hatch)"/>
        <line x1="${x0.toFixed(1)}" y1="${PAD_T}" x2="${x0.toFixed(1)}" y2="${PAD_T + PH}"
          stroke="var(--pvs-cell-stroke)" stroke-width="1" stroke-dasharray="3 2"/>
        <text class="axis" x="${(x0 + 5).toFixed(1)}" y="${PAD_T + 10}">${t(hass, "curve_unreachable_band")}</text>`;
    }

    const seg = (sel, filter = () => true) => {
      let d = "", pen = false;
      for (const p of pts) {
        const v = sel(p);
        if (v == null || !filter(p)) { pen = false; continue; }
        d += `${pen ? "L" : "M"}${xOf(p.load).toFixed(1)} ${yOf(v).toFixed(1)}`;
        pen = true;
      }
      return d;
    };
    const hasPrior = pts.some((p) => p.prior != null);
    // Nothing learned yet: the applied curve IS the prior, so it carries the
    // prior's dashed style — a solid "learned" line would claim a measurement
    // that has not been accepted.
    const appliedStroke = anyLearned
      ? `stroke="var(--pvs-model)" stroke-width="2.4"`
      : `stroke="var(--pvs-model)" stroke-width="2" stroke-dasharray="5 3"`;
    // measurement in the measurement colour (as everywhere in this dashboard),
    // with its scatter as a whisker where the integration reports one
    let measured = "";
    if (anyMeasured) {
      measured += `<path d="${seg((p) => p.measured)}" fill="none" stroke="var(--pvs-measure)"
        stroke-width="1.6" stroke-linejoin="round" opacity="0.85"/>`;
      for (const p of pts) {
        if (p.measured == null) continue;
        const x = xOf(p.load).toFixed(1);
        if (p.spread) {
          measured += `<line x1="${x}" y1="${yOf(Math.min(1, p.measured + p.spread)).toFixed(1)}"
            x2="${x}" y2="${yOf(Math.max(0, p.measured - p.spread)).toFixed(1)}"
            stroke="var(--pvs-measure)" stroke-width="1" opacity="0.5"/>`;
        }
        measured += `<circle cx="${x}" cy="${yOf(p.measured).toFixed(1)}" r="2.8"
          fill="var(--pvs-measure)"/>`;
      }
    }
    const marks = pts.map((p) => `<circle cx="${xOf(p.load).toFixed(1)}" cy="${yOf(p.eta).toFixed(1)}"
      r="${p.learned ? 3.6 : 3}" fill="${p.learned ? "var(--pvs-model)" : "var(--card-background-color)"}"
      stroke="${p.reachable ? "var(--pvs-model)" : "var(--pvs-model-ghost)"}" stroke-width="1.4"/>`).join("");
    const hits = pts.map((p) => `<rect x="${(xOf(p.load) - 9).toFixed(1)}" y="${PAD_T}" width="18"
      height="${STRIP_Y + STRIP_H - PAD_T}" fill="transparent" data-pt='${esc(JSON.stringify({ ...p, maxLoad, blocked }))}'/>`).join("");

    // delta strip: eta - prior in percentage points, zero line centred
    let strip = "";
    if (anyLearned) {
      const deltas = pts.filter((p) => p.learned && p.prior != null)
        .map((p) => (p.eta - p.prior) * 100);
      const dMax = Math.max(0.5, ...deltas.map(Math.abs));
      const zeroY = STRIP_Y + STRIP_H / 2;
      const dOf = (d) => zeroY - (d / dMax) * (STRIP_H / 2 - 3);
      strip += `<line class="grid" x1="${PAD_L}" y1="${zeroY}" x2="${W - PAD_R}" y2="${zeroY}"/>
        <text class="axis" x="${PAD_L - 5}" y="${STRIP_Y + 8}" text-anchor="end">+${fmtNum(hass, dMax, 1)}</text>
        <text class="axis" x="${PAD_L - 5}" y="${STRIP_Y + STRIP_H}" text-anchor="end">−${fmtNum(hass, dMax, 1)}</text>
        <text class="axis" x="${W - PAD_R + 3}" y="${zeroY + 3}" style="font-size:8.5px">pp</text>`;
      for (const p of pts) {
        if (!p.learned || p.prior == null) continue;
        const d = (p.eta - p.prior) * 100;
        const y = dOf(d), x = xOf(p.load);
        strip += `<rect x="${(x - 5).toFixed(1)}" y="${Math.min(y, zeroY).toFixed(1)}"
          width="10" height="${Math.max(1, Math.abs(y - zeroY)).toFixed(1)}"
          fill="var(--pvs-model)" opacity="0.55" rx="1"/>`;
      }
    }

    // State line above the chart. Blocked is a property of the hardware, not a
    // fault: it gets a calm note, never the warning style.
    let stateNote = "";
    if (blocked) {
      const known = blocked === "output_appears_derived_from_input";
      stateNote = `<div class="kv-note">${known ? t(hass, "curve_blocked_derived")
        : t(hass, "curve_blocked_other", { reason: esc(blocked) })}</div>`;
    } else if (!anyLearned) {
      stateNote = `<div class="kv-note">${anyMeasured
        ? t(hass, "curve_collecting") : t(hass, "curve_gathering")}</div>`;
    }

    card(`
      ${head(chips.join(""))}
      ${stateNote}
      <div class="fc-wrap"><svg class="fc-line" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="aspect-ratio:${W}/${H}">
        <defs>${hatchPattern("pvs-curve-hatch")}${hatchPattern("pvs-curve-hatch-l")}</defs>
        ${grid}${unreach}
        ${hasPrior && anyLearned ? `<path d="${seg((p) => p.prior)}" fill="none" stroke="var(--pvs-model-ghost)"
          stroke-width="1.8" stroke-dasharray="4 3" stroke-linejoin="round"/>` : ""}
        <path d="${seg((p) => p.eta)}" fill="none" ${appliedStroke} stroke-linejoin="round"/>
        ${measured}${marks}${strip}${hits}
      </svg></div>
      <div class="pvs-legend">
        ${hasPrior && anyLearned ? `<span class="it"><span class="sw dash" style="background:var(--pvs-model-ghost)"></span>${t(hass, "curve_prior_series")}</span>` : ""}
        <span class="it"><span class="sw" style="background:var(--pvs-model)"></span>${anyLearned ? t(hass, "curve_learned") : t(hass, "curve_applied")}</span>
        ${anyMeasured ? `<span class="it"><span class="sw" style="background:var(--pvs-measure)"></span>${t(hass, "curve_measured")}</span>` : ""}
        ${firstUnreach ? `<span class="it"><svg width="14" height="12"><rect x="0.5" y="0.5" width="13" height="11" rx="2" fill="url(#pvs-curve-hatch-l)" stroke="var(--pvs-cell-stroke)" stroke-width="1"/></svg>${t(hass, "curve_unreachable")}</span>` : ""}
        ${anyLearned ? `<span class="it"><span class="sw" style="background:var(--pvs-model);opacity:0.55"></span>${t(hass, "curve_delta")}</span>` : ""}
      </div>
      <div class="kv-note dim">${t(hass, "curve_note")}</div>`);
  }

  _wire() {
    this._wireMoreInfo();
    if (this._wired) return;
    this._wired = true;
    wireTooltip(this, {
      selector: "[data-pt]",
      content: (el) => {
        const hass = this._hass;
        const p = JSON.parse(el.getAttribute("data-pt"));
        const delta = p.prior != null ? (p.eta - p.prior) * 100 : null;
        // status, most specific first: never reached beats "no evidence",
        // and a blocked curve beats "still gathering"
        const status = !p.reachable
          ? t(hass, "curve_point_unreachable", { max: p.maxLoad != null ? fmtNum(hass, p.maxLoad * 100, 0) : "?" })
          : p.blocked ? t(hass, "curve_point_blocked")
          : p.learned ? t(hass, "curve_point_learned")
          : p.measured != null ? t(hass, "curve_point_moving")
          : t(hass, "curve_point_nodata");
        return `<div class="h">${fmtNum(hass, p.load * 100, p.load < 0.1 ? 1 : 0)} % ${t(hass, "curve_tip_load")}</div>
          <div class="r"><span class="k">${t(hass, "curve_applied")}</span><span class="v">${fmtNum(hass, p.eta * 100, 2)} %</span></div>
          ${p.prior != null ? `<div class="r"><span class="k">${t(hass, "curve_prior_series")}</span><span class="v">${fmtNum(hass, p.prior * 100, 2)} %</span></div>` : ""}
          ${p.measured != null ? `<div class="r"><span class="k">${t(hass, "curve_measured")}</span><span class="v" style="color:var(--pvs-measure)">${fmtNum(hass, p.measured * 100, 2)} %${p.spread ? ` ± ${fmtNum(hass, p.spread * 100, 2)}` : ""}</span></div>` : ""}
          ${delta != null && p.learned ? `<div class="r"><span class="k">Δ</span><span class="v">${delta > 0 ? "+" : ""}${fmtNum(hass, delta, 2)} pp</span></div>` : ""}
          <div class="r"><span class="k">n_eff</span><span class="v">${fmtNum(hass, p.n, 1)}</span></div>
          <div class="pvs-sub">${status}</div>`;
      },
    });
  }

  static getConfigElement() { return document.createElement("pvstrings-chain-editor"); }
  static getStubConfig() { return { entity: "" }; }
}

const CURVE_CSS = `
  .pvs-legend .sw.dash { height: 2px; border-radius: 0; }
  .kv-note { font-size: 11px; padding: 5px 8px; border-radius: 6px;
    background: var(--pvs-chip-bg); color: var(--secondary-text-color); margin-top: 8px; }
  .kv-note.warn { background: color-mix(in srgb, var(--warning-color, #ffa600) 12%, transparent); color: var(--primary-text-color); }
  .kv-note.dim { color: var(--secondary-text-color); }
`;

/* ========================= SECTION: CARD:CHAIN =========================== */

class PvsChainCard extends PvsBaseCard {
  setConfig(config) {
    this._hourMs = null; // null = follow "now"
    this._measured = undefined;
    super.setConfig(config ?? {});
  }
  getCardSize() { return 4; }

  async _loadMeasured(hourMs) {
    const hass = this._hass;
    const token = ++this._renderToken;
    this._measured = undefined;
    try {
      const producedId = await sibling(hass, this._config.entity, "string_produced_today");
      if (!producedId) { this._measured = { err: "unresolved" }; return this._render(); }
      this._producedId = producedId;
      const rows = await hourlyActuals(hass, producedId,
        new Date(hourMs).toISOString(), new Date(hourMs + 3600000).toISOString());
      if (token !== this._renderToken) return;
      const row = rows.find((x) => x.startMs === hourMs);
      this._measured = { kwh: row?.change ?? null, hourMs };
      this._render();
    } catch (_) {
      if (token === this._renderToken) { this._measured = { err: "stats" }; this._render(); }
    }
  }

  _render() {
    const hass = this._hass, cfg = this._config;
    if (!hass || !cfg) return;
    const card = (inner) => {
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${CHAIN_CSS}</style><ha-card>${inner}</ha-card>`;
      this._wire();
    };
    if (!cfg.entity) return card(problemHTML(hass, { reason: t(hass, "no_entity_config") }));
    const st = hass.states[cfg.entity];
    if (!st) return card(problemHTML(hass, { reason: t(hass, "entity_missing", { entity: cfg.entity }) }));
    const need = requireFeatures(st, ["forecast_list", "chain_steps"]);
    if (!need.ok) {
      return card(problemHTML(hass, {
        entity: cfg.entity, missing: need.missing,
        hint: t(hass, "chain_needs_string"),
      }));
    }
    const rows = (st.attributes.forecast ?? [])
      .map((r) => ({ ...r, ms: new Date(r.datetime).getTime() }))
      .sort((a, b) => a.ms - b.ms);
    if (!rows.length) {
      return card(`<div class="pvs-head"><span class="pvs-title">${t(hass, "chain_title")}</span></div>
        ${withheldHTML(t(hass, "fc_no_hours"))}`);
    }
    const nowMs = Date.now(), curHourMs = nowMs - (nowMs % 3600000);
    let hourMs = this._hourMs ?? curHourMs;
    if (!rows.some((r) => r.ms === hourMs)) {
      // nearest available row
      hourMs = rows.reduce((best, r) =>
        Math.abs(r.ms - hourMs) < Math.abs(best - hourMs) ? r.ms : best, rows[0].ms);
    }
    const row = rows.find((r) => r.ms === hourMs);
    const idx = rows.indexOf(row);
    if (this._measured?.hourMs !== hourMs && hourMs + 3600000 <= nowMs + 3600000) {
      if (this._measured === undefined || this._measured?.hourMs !== hourMs) {
        if (hourMs <= curHourMs) this._loadMeasured(hourMs);
      }
    }

    const title = st.attributes.friendly_name?.replace(/ (Prognose heute|Forecast today)$/i, "") ?? cfg.entity;
    const isFuture = hourMs > curHourMs, isCur = hourMs === curHourMs;

    const product = row.physics_kwh * row.shading * row.model;
    const mismatch = Math.abs(product - row.potential_kwh) > 1e-3;
    const afterShading = row.physics_kwh * row.shading;

    const meter = (f) => {
      const w = Math.max(0, Math.min(1, f)) * 56;
      return `<svg width="56" height="6"><rect width="56" height="6" rx="3" fill="var(--pvs-chip-bg)"/>
        <rect width="${w}" height="6" rx="3" fill="var(--pvs-model)"/></svg>`;
    };
    const kwhCell = (v) => `<span class="pvs-num v">${fmtNum(hass, v, 3)}</span><span class="unit">kWh</span>`;

    let measuredHtml;
    if (isFuture) {
      measuredHtml = withheldHTML(t(hass, "not_yet_hour"));
    } else if (this._measured === undefined) {
      measuredHtml = `<span class="pvs-sub">…</span>`;
    } else if (this._measured.err) {
      measuredHtml = `<span class="pvs-sub">⚠︎ ${t(hass, "stats_unavailable", { entity: this._producedId ?? cfg.entity })}</span>`;
    } else if (this._measured.kwh == null) {
      measuredHtml = withheldHTML(isCur
        ? t(hass, "in_progress", { min: new Date().getMinutes() })
        : t(hass, "not_available"));
    } else {
      const delta = this._measured.kwh - row.potential_kwh;
      measuredHtml = `${kwhCell(this._measured.kwh)}
        <span class="delta ${delta >= 0 ? "up" : "down"}">${fmtSigned(hass, delta, 3)} kWh</span>
        ${isCur ? `<span class="pvs-sub">· ${t(hass, "in_progress", { min: new Date().getMinutes() })}</span>` : ""}`;
    }

    card(`
      <div class="pvs-head">
        <span class="pvs-title">${esc(title)} — ${t(hass, "chain_title")}</span>
        <span class="ch-stepper">
          <button data-step="-1" ${idx === 0 ? "disabled" : ""}>◀</button>
          <span class="pvs-num">${fmtWeekday(hass, hourMs)} ${fmtHour(hass, hourMs)}</span>
          <button data-step="1" ${idx === rows.length - 1 ? "disabled" : ""}>▶</button>
          <button data-step="0" title="${t(hass, "now")}" ${this._hourMs == null ? "disabled" : ""}>⦿</button>
        </span>
      </div>
      <div class="ch-bias">${t(hass, "chain_source_bias", { v: fmtNum(hass, row.source_bias, 3) })}</div>
      ${row.thermal !== undefined && row.cell_temp_c != null
        // heat share (>= 1.24): context like the source bias, never a link —
        // physics_kwh already carries it, multiplying it in again would be
        // exactly the mistake the integration has a test against
        ? `<div class="ch-bias ch-thermal ${row.thermal < 0.97 ? "warm" : ""}">${t(hass, "chain_thermal", {
            v: fmtNum(hass, row.thermal, 3), cell: fmtNum(hass, row.cell_temp_c, 1),
            air: fmtNum(hass, row.air_temp_c, 1), wind: fmtNum(hass, row.wind_ms, 1) })}</div>`
        : ""}
      <div class="ch-grid">
        <div class="lab" data-more-info="${cfg.entity}">${t(hass, "chain_physics")}</div>
        <div></div><div></div>
        <div class="val">${kwhCell(row.physics_kwh)}</div>

        <div class="lab op">× ${t(hass, "chain_shading")}</div>
        <div class="fac"><span class="pvs-num">${fmtNum(hass, row.shading, 3)}</span>${meter(row.shading)}</div>
        <div class="eff pvs-num">${fmtSigned(hass, afterShading - row.physics_kwh, 3)}</div>
        <div class="val dim">${kwhCell(afterShading)}</div>

        <div class="lab op">× ${t(hass, "chain_model")}</div>
        <div class="fac"><span class="pvs-num">${fmtNum(hass, row.model, 3)}</span>${meter(row.model)}</div>
        <div class="eff pvs-num">${fmtSigned(hass, product - afterShading, 3)}</div>
        <div class="val dim">${kwhCell(product)}</div>

        <div class="lab pub" data-more-info="${cfg.entity}">= ${t(hass, "chain_published")}</div>
        <div></div><div></div>
        <div class="val pub">${kwhCell(row.potential_kwh)}</div>

        ${mismatch ? `<div class="ch-mismatch">⚠︎ ${t(hass, "chain_discrepancy", { a: fmtNum(hass, product, 4), b: fmtNum(hass, row.potential_kwh, 4) })}</div>` : ""}

        <div class="lab meas" ${this._producedId ? `data-more-info="${this._producedId}"` : ""}>${t(hass, "chain_measured")}</div>
        <div class="meas-val">${measuredHtml}</div>
      </div>`);
  }

  _wire() {
    this._wireMoreInfo();
    if (this._wired) return;
    this._wired = true;
    this.shadowRoot.addEventListener("click", (ev) => {
      const b = ev.target.closest?.("[data-step]");
      if (!b || b.disabled) return;
      const step = parseInt(b.getAttribute("data-step"), 10);
      const st = this._hass.states[this._config.entity];
      const rows = (st?.attributes?.forecast ?? [])
        .map((r) => new Date(r.datetime).getTime()).sort((a, b2) => a - b2);
      if (step === 0) { this._hourMs = null; this._measured = undefined; this._render(); return; }
      const nowMs = Date.now();
      const cur = this._hourMs ?? nowMs - (nowMs % 3600000);
      const curIdx = rows.findIndex((ms) => ms >= cur);
      const next = rows[Math.max(0, Math.min(rows.length - 1,
        (curIdx === -1 ? rows.length - 1 : curIdx) + step))];
      if (next != null) { this._hourMs = next; this._measured = undefined; this._render(); }
    });
  }

  static getConfigElement() { return document.createElement("pvstrings-chain-editor"); }
  static getStubConfig(hass, entities) {
    const guess = (entities ?? []).find((e) =>
      hass.states[e]?.attributes?.forecast?.[0]?.physics_kwh !== undefined);
    return { entity: guess ?? "" };
  }
}

const CHAIN_CSS = `
  .ch-stepper { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; }
  .ch-stepper button {
    all: unset; cursor: pointer; padding: 3px 7px; border-radius: 5px;
    font-size: 10px; color: var(--secondary-text-color); background: var(--pvs-chip-bg);
    transition: color 150ms;
  }
  .ch-stepper button:hover:not([disabled]) { color: var(--primary-text-color); }
  .ch-stepper button[disabled] { opacity: 0.35; cursor: default; }
  .ch-bias {
    font-size: 11px; line-height: 1.5; color: var(--secondary-text-color);
    padding: 6px 9px; margin-bottom: 12px; border-radius: 6px;
    background: var(--pvs-chip-bg); font-style: italic;
  }
  .ch-bias + .ch-thermal { margin-top: -7px; }
  .ch-thermal.warm { background: color-mix(in srgb, var(--pvs-measure) 10%, transparent); }
  .ch-grid {
    display: grid; grid-template-columns: minmax(90px, 1fr) auto auto auto;
    gap: 7px 14px; align-items: center; font-size: 12.5px;
  }
  .ch-grid .lab { color: var(--primary-text-color); }
  .ch-grid .lab[data-more-info] { cursor: pointer; text-decoration: underline dotted color-mix(in srgb, currentColor 40%, transparent); text-underline-offset: 3px; }
  .ch-grid .lab.op { color: var(--secondary-text-color); padding-left: 12px; }
  .ch-grid .fac { display: inline-flex; align-items: center; gap: 8px; font-size: 12px; }
  .ch-grid .eff { text-align: right; color: var(--secondary-text-color); font-size: 11.5px; }
  .ch-grid .val { text-align: right; white-space: nowrap; }
  .ch-grid .val .v { font-size: 14px; }
  .ch-grid .val.dim .v { color: var(--secondary-text-color); font-size: 12.5px; }
  .ch-grid .val .unit { font-size: 10px; color: var(--secondary-text-color); margin-left: 4px; }
  .ch-grid .pub { font-weight: 700; }
  .ch-grid .val.pub { border-top: 1px solid var(--pvs-hairline); padding-top: 7px; }
  .ch-grid .val.pub .v { font-size: 16px; color: var(--pvs-model); }
  .ch-grid .lab.pub { border-top: 1px solid var(--pvs-hairline); padding-top: 7px; }
  .ch-grid .meas { color: var(--pvs-measure); font-weight: 600; margin-top: 4px; }
  .ch-grid .meas-val { grid-column: 2 / 5; text-align: right; margin-top: 4px; white-space: nowrap; }
  .ch-grid .meas-val .v { font-size: 15px; color: var(--pvs-measure); }
  .ch-grid .meas-val .unit { font-size: 10px; color: var(--secondary-text-color); margin-left: 4px; }
  .delta { font-family: var(--pvs-mono); font-size: 11.5px; margin-left: 10px; }
  .delta.up { color: var(--success-color, #0ca30c); }
  .delta.down { color: var(--error-color, #d03b3b); }
  .ch-mismatch {
    grid-column: 1 / 5; font-size: 11.5px; line-height: 1.5; padding: 7px 9px;
    border-radius: 6px; color: var(--primary-text-color);
    background: color-mix(in srgb, var(--error-color, #d03b3b) 10%, transparent);
    border: 1px solid color-mix(in srgb, var(--error-color, #d03b3b) 40%, transparent);
  }
`;

/* ========================= SECTION: CARD:DAILY =========================== */

class PvsDailyCard extends PvsBaseCard {
  setConfig(config) {
    this._data = null;
    this._problem = null;
    this._sel = 0;
    this._cache = new Map();
    super.setConfig(config ?? {});
  }
  getCardSize() { return 4; }
  getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }
  _shouldUpdate(prev) { return !prev; } // recorder data; no per-state refresh

  // `series` (plant first, then the strings) makes one card of six: the
  // chips in the head switch, loaded data is kept per entity. Without it the
  // card is what it was — one entity, `days`.
  _current() {
    const cfg = this._config ?? {};
    const s = Array.isArray(cfg.series) && cfg.series.length ? cfg.series : null;
    if (!s) return { entity: cfg.entity, days: cfg.days ?? 14, title: cfg.title };
    const cur = s[Math.max(0, Math.min(s.length - 1, this._sel))];
    return { entity: cur.entity, days: cur.days ?? cfg.days ?? 14, title: cur.title ?? cur.name };
  }

  async _load() {
    const hass = this._hass, cfg = { ...this._config, ...this._current() };
    if (this._loading) return;
    this._loading = true;
    const token = ++this._renderToken;
    try {
      const m = await getRegistryModel(hass);
      const info = m.byEntityId.get(cfg.entity);
      if (!info) { this._problem = { reason: t(hass, "entity_missing", { entity: cfg.entity }) }; return; }
      const isString = info.level === "string";
      const tomorrowKey = isString ? "string_forecast_tomorrow" : "forecast_tomorrow";
      const producedKey = isString ? "string_produced_today" : "produced_today";
      const tomorrowId = info.node.byKey[tomorrowKey];
      const producedId = info.node.byKey[producedKey];
      if (!tomorrowId || !producedId) {
        this._problem = {
          entity: cfg.entity,
          reason: `${t(hass, "not_available")}: ${!tomorrowId ? tomorrowKey : producedKey}`,
        };
        return;
      }
      // issue hour from the plant's day-ahead sensors (fallback 18)
      let issueHour = 18;
      for (const key of ["wmape_day_ahead_30d", "wmape_day_ahead_7d", "deviation_yesterday"]) {
        const id = await plantSibling(hass, cfg.entity, key);
        const v = id && hass.states[id]?.attributes?.issue_hour_local;
        if (typeof v === "number") { issueHour = v; break; }
      }
      const days = cfg.days ?? 14;
      const plantTomorrowId = isString
        ? await plantSibling(hass, cfg.entity, "forecast_tomorrow") : null;
      const scope = {
        entityId: cfg.entity,
        tomorrowId,
        stringName: isString ? info.node.name : null,
      };
      const [actualRows, issued, plantIssued] = await Promise.all([
        dailyActuals(hass, producedId, days),
        issuedForecasts(hass, scope, days, issueHour),
        plantTomorrowId
          ? issuedForecasts(
              hass,
              { entityId: cfg.entity, tomorrowId: plantTomorrowId, stringName: null },
              days,
              issueHour,
            )
          : null,
      ]);
      if (token !== this._renderToken) return;
      const actualByDay = new Map();
      for (const r of actualRows) {
        if (r.change != null) actualByDay.set(localParts(hass, r.startMs).dayKey, r.change);
      }
      const out = [];
      const todayKey = localParts(hass, Date.now()).dayKey;
      for (let back = days - 1; back >= 0; back--) {
        const dayMs = localMidnightMs(hass, Date.now(), back) + 12 * 3600000;
        const dayKey = localParts(hass, dayMs).dayKey;
        const evePrevKey = localParts(hass, localMidnightMs(hass, Date.now(), back + 1) + 12 * 3600000).dayKey;
        let soll = issued.get(evePrevKey) ?? null;
        // string_forecast_tomorrow publishes 0.0 instead of None when no rows
        // exist; the plant-level bucket is the tie-breaker (known limitation).
        const plantMissing = plantIssued ? !plantIssued.has(evePrevKey) : false;
        const noIssue = soll == null || (isString && plantMissing);
        if (noIssue) soll = null;
        out.push({
          dayMs, dayKey, evePrevKey, soll,
          ist: actualByDay.get(dayKey) ?? null,
          isToday: dayKey === todayKey,
        });
      }
      const anyStats = actualRows.length > 0 || issued.size > 0;
      this._data = {
        rows: out, issueHour, tomorrowId, producedId, anyStats,
        sollSource: issued.source ?? "statistics",
      };
      this._problem = anyStats ? null
        : { entity: `${tomorrowId}, ${producedId}`, reason: t(hass, "stats_unavailable", { entity: `${tomorrowId} / ${producedId}` }) };
    } catch (e) {
      this._problem = { reason: String(e) };
    } finally {
      this._loading = false;
      this._cache.set(cfg.entity, { data: this._data, problem: this._problem });
      if (token === this._renderToken) this._render();
    }
  }

  _render() {
    const hass = this._hass, cfg = { ...this._config, ...this._current() };
    if (!hass || !this._config) return;
    const card = (inner) => {
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${DAILY_CSS}</style><ha-card>${inner}<div class="pvs-tip"></div></ha-card>`;
      this._wire();
    };
    if (!cfg.entity) return card(problemHTML(hass, { reason: t(hass, "no_entity_config") }));
    if (!hass.states[cfg.entity]) return card(problemHTML(hass, { reason: t(hass, "entity_missing", { entity: cfg.entity }) }));
    const series = Array.isArray(this._config.series) && this._config.series.length ? this._config.series : null;
    const title = series
      ? (this._config.title ?? t(hass, "daily_title"))
      : cfg.title ?? `${hass.states[cfg.entity].attributes.friendly_name?.split(" Prognose")[0]?.split(" Forecast")[0] ?? ""} — ${t(hass, "daily_title")}`;
    const selector = series ? `<div class="d-sel">${series.map((s, i) =>
      `<span class="pvs-chip${i === this._sel ? " on" : ""}" data-sel="${i}">${esc(s.name ?? s.entity)}</span>`).join("")}</div>` : "";
    const headHTML = (chips = "") => `<div class="pvs-head"><span class="pvs-title">${esc(title)}</span>${chips}${helpChip("daily")}</div>${selector}`;

    if (!this._data && !this._problem) {
      this._load();
      return card(`${headHTML()}<div class="pvs-sub">…</div>`);
    }
    if (this._problem) return card(headHTML() + problemHTML(hass, this._problem));

    const { rows, issueHour, tomorrowId, producedId } = this._data;
    let maxV = 0;
    for (const r of rows) maxV = Math.max(maxV, r.soll ?? 0, r.ist ?? 0);
    const yMax = niceMax(maxV * 1.05 || 1);
    const SW = 30, BW = 11, PAD_L = 30, PAD_R = 4, PAD_T = 8, PAD_B = 22, PH = 120;
    const W = PAD_L + rows.length * SW + PAD_R, H = PAD_T + PH + PAD_B;
    const yOf = (v) => PAD_T + PH - (v / yMax) * PH;

    let bars = "", hits = "", labels = "";
    rows.forEach((r, i) => {
      const x0 = PAD_L + i * SW;
      if (r.soll != null && r.soll > 0) {
        bars += `<rect x="${x0 + SW / 2 - BW - 1}" y="${yOf(r.soll)}" width="${BW}"
          height="${Math.max(1, PAD_T + PH - yOf(r.soll))}" rx="2" fill="var(--pvs-model)"/>`;
      } else if (r.soll == null) {
        bars += `<rect x="${x0 + SW / 2 - BW - 1}" y="${PAD_T + PH - 14}" width="${BW}" height="14"
          rx="2" fill="var(--pvs-unobserved)"/>
          <rect x="${x0 + SW / 2 - BW - 1}" y="${PAD_T + PH - 14}" width="${BW}" height="14"
          rx="2" fill="url(#pvs-hatch-d)"/>`;
      }
      if (r.ist != null && r.ist > 0) {
        const iy = yOf(r.ist);
        bars += `<rect x="${x0 + SW / 2 + 1}" y="${iy}" width="${BW}"
          height="${Math.max(1, PAD_T + PH - iy)}" rx="2" fill="var(--pvs-measure)"/>`;
        if (r.isToday) {
          bars += `<rect x="${x0 + SW / 2 + 1}" y="${iy}" width="${BW}"
            height="${Math.min(8, PAD_T + PH - iy)}" fill="url(#pvs-hatch-d)"/>`;
        }
      }
      if (i % 2 === (rows.length - 1) % 2) {
        labels += `<text x="${x0 + SW / 2}" y="${H - 8}" text-anchor="middle">${fmtDayShort(hass, r.dayMs)}</text>`;
      }
      hits += `<rect class="hit" x="${x0}" y="${PAD_T}" width="${SW}" height="${PH}"
        fill="transparent" data-day='${esc(JSON.stringify(r))}'/>`;
    });

    let yaxis = "";
    for (const v of [0, yMax / 2, yMax]) {
      yaxis += `<line class="grid" x1="${PAD_L}" y1="${yOf(v)}" x2="${W - PAD_R}" y2="${yOf(v)}"/>
        <text class="axis" x="${PAD_L - 4}" y="${yOf(v) + 3}" text-anchor="end">${fmtNum(hass, v, 0)}</text>`;
    }

    const scored = rows.filter((r) => r.soll != null && r.ist != null && !r.isToday);
    const sumSoll = scored.reduce((s, r) => s + r.soll, 0);
    const sumIst = scored.reduce((s, r) => s + r.ist, 0);
    const wabs = sumIst > 0
      ? scored.reduce((s, r) => s + Math.abs(r.ist - r.soll), 0) / sumIst * 100 : null;

    const legend = `<div class="pvs-legend">
      <span class="it"><span class="sw" style="background:var(--pvs-model)"></span>${t(hass, "daily_soll")}</span>
      <span class="it"><span class="sw" style="background:var(--pvs-measure)"></span>${t(hass, "daily_ist")}</span>
      <span class="it"><svg width="14" height="10"><rect width="14" height="10" rx="2" fill="var(--pvs-unobserved)"/><rect width="14" height="10" rx="2" fill="url(#pvs-hatch-dl)"/></svg>${t(hass, "daily_no_issue")}</span>
      ${scored.length ? `<span class="it" style="margin-left:auto">${t(hass, "daily_window_sum", { n: scored.length, soll: fmtKwh(hass, sumSoll, 1), ist: fmtKwh(hass, sumIst, 1) })}${wabs != null ? ` · ${t(hass, "daily_wabs")} <span class="pvs-num">${fmtNum(hass, wabs, 1)} %</span>` : ""}</span>` : ""}
    </div>`;

    card(`
      ${headHTML(`<span class="pvs-chip clickable" data-more-info="${tomorrowId}" title="${esc(t(hass, "more_info"))}: ${tomorrowId}">${t(hass, "daily_soll").split(" ")[0]} ↗</span>
        <span class="pvs-chip clickable" data-more-info="${producedId}" title="${esc(t(hass, "more_info"))}: ${producedId}">${t(hass, "daily_ist")} ↗</span>`)}
      <div class="d-wrap"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="aspect-ratio:${W}/${H}">
        <defs>${hatchPattern("pvs-hatch-d")}${hatchPattern("pvs-hatch-dl")}</defs>
        ${yaxis}${bars}${hits}
      </svg></div>
      ${legend}`);
  }

  _wire() {
    this._wireMoreInfo();
    if (this._wired) return;
    this._wired = true;
    this.shadowRoot.addEventListener("click", (ev) => {
      const c = ev.target.closest?.("[data-sel]");
      if (!c) return;
      this._sel = parseInt(c.getAttribute("data-sel"), 10);
      const hit = this._cache.get(this._current().entity);
      this._data = hit?.data ?? null; this._problem = hit?.problem ?? null;
      this._render();
    });
    wireTooltip(this, {
      selector: ".hit",
      content: (el) => {
        const hass = this._hass;
        const r = JSON.parse(el.getAttribute("data-day"));
        const delta = (r.soll != null && r.ist != null) ? r.ist - r.soll : null;
        const issueHour = this._data?.issueHour ?? 18;
        return `<div class="h">${fmtWeekday(hass, r.dayMs)} ${fmtDayShort(hass, r.dayMs)}${r.isToday ? ` · ${t(hass, "day_running")}` : ""}</div>
          <div class="r"><span class="k">${t(hass, "daily_soll")}</span><span class="v">${r.soll == null ? "—" : fmtKwh(hass, r.soll)}</span></div>
          <div class="r"><span class="k">${t(hass, "daily_ist")}</span><span class="v">${r.ist == null ? "—" : fmtKwh(hass, r.ist)}</span></div>
          ${delta != null && !r.isToday ? `<div class="r"><span class="k">Δ</span><span class="v">${fmtSigned(hass, delta)} kWh (${fmtSigned(hass, r.soll > 0 ? delta / r.soll * 100 : 0, 0)} %)</span></div>` : ""}
          ${r.soll == null ? `<div class="pvs-sub">${t(hass, "daily_no_issue")}</div>` : `<div class="pvs-sub">${t(hass, this._data?.sollSource === "integration" ? "daily_provenance_own" : "daily_provenance", { entity: "forecast_tomorrow", date: r.evePrevKey, hour: issueHour })}</div>`}`;
      },
    });
  }

  static getConfigElement() { return document.createElement("pvstrings-daily-editor"); }
  static getStubConfig(hass, entities) {
    const guess = (entities ?? []).find((e) =>
      Array.isArray(hass.states[e]?.attributes?.forecast));
    return { entity: guess ?? "", days: 14 };
  }
}

const DAILY_CSS = `
  .d-wrap svg { width: 100%; height: auto; display: block; }
  .hit { cursor: crosshair; }
  .d-sel { display: flex; gap: 6px; flex-wrap: wrap; margin: 0 0 8px; }
  .d-sel .pvs-chip { cursor: pointer; border-color: var(--pvs-hairline); }
  .d-sel .pvs-chip.on { color: var(--primary-text-color); border-color: var(--pvs-model);
    background: color-mix(in srgb, var(--pvs-model) 14%, transparent); }
`;

/* ========================= SECTION: CARD:ACCURACY ======================== */

// The seven accuracy figures as one card: short-term on one line, day-ahead
// on the other, each with how full its window is — that fill decides whether
// the two may be compared at all, so it stands beside the numbers instead of
// in a paragraph above them. The paragraphs live behind the "?".
const ACC_KEYS = {
  short: ["wmape_7d", "wmape_30d", "bias_7d"],
  dayahead: ["wmape_day_ahead_7d", "wmape_day_ahead_30d", "bias_day_ahead_30d", "deviation_yesterday"],
};
const ACC_LABEL = {
  wmape_7d: "acc_7d", wmape_30d: "acc_30d", bias_7d: "acc_bias7",
  wmape_day_ahead_7d: "acc_7d", wmape_day_ahead_30d: "acc_30d",
  bias_day_ahead_30d: "acc_bias30", deviation_yesterday: "acc_yesterday",
};

class PvsAccuracyCard extends PvsBaseCard {
  getCardSize() { return 3; }
  getGridOptions() { return { columns: "full", rows: "auto" }; }
  watchedEntities() { return Object.values(this._config?.entities ?? {}).filter(Boolean); }

  _render() {
    const hass = this._hass, cfg = this._config;
    if (!hass || !cfg) return;
    const ents = cfg.entities ?? {};
    const card = (inner) => {
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${ACC_CSS}</style><ha-card>${inner}<div class="pvs-tip"></div></ha-card>`;
      this._wireMoreInfo();
      if (!this._wired) { this._wired = true; wireTooltip(this); }
    };
    if (!Object.values(ents).some(Boolean)) return card(problemHTML(hass, { reason: t(hass, "no_entity_config") }));

    const stat = (key) => {
      const id = ents[key];
      if (!id) return "";
      const st = hass.states[id];
      const label = t(hass, ACC_LABEL[key]);
      if (!st) return `<div class="acc-stat"><div class="l">${label}</div><div class="v"><span class="pvs-sub">⚠︎ ${esc(id)}</span></div></div>`;
      let v = parseFloat(st.state);
      let attrs = st.attributes;
      const unit = attrs.unit_of_measurement ?? "";
      // The bias_7d state is a mean over single HOURS (-0.005 kWh, which
      // rounds to -0.00), while its day-ahead neighbour is per DAY. The same
      // window's per-day figure sits in the 7-day WMAPE sensor's attributes —
      // read it from there, so the two biases on this card mean the same
      // thing, and take that sensor's day count with it.
      if (key === "bias_7d") {
        const w = hass.states[ents.wmape_7d]?.attributes;
        if (w && w.uncensored && "daily_bias_kwh" in w.uncensored) {
          v = w.uncensored.daily_bias_kwh == null ? NaN : Number(w.uncensored.daily_bias_kwh);
          attrs = w;
        }
      }
      const known = Number.isFinite(v);
      // a withheld figure says how far off it is (rule 1): days scored
      // against the window the key names
      const win = key.includes("30d") ? 30 : key.includes("7d") ? 7 : null;
      // the sensor counts partial days at both ends, so "8 of 7" happens;
      // a full window is full
      const days = attrs.days_scored == null ? null
        : win ? Math.min(win, attrs.days_scored) : attrs.days_scored;
      const sub = win && days != null ? `<span class="n">${t(hass, "acc_window", { d: days, n: win })}</span>` : "";
      const digits = unit === "%" ? 1 : 2;
      // everything but WMAPE is signed: its sign is the finding
      return `<div class="acc-stat clickable" data-more-info="${id}">
        <div class="l">${label}</div>
        <div class="v">${known
          ? `<span class="pvs-num">${key.startsWith("wmape") ? fmtNum(hass, v, digits) : fmtSigned(hass, v, digits)}</span><span class="u">${esc(unit)}</span>`
          : withheldHTML(days != null && win ? t(hass, "acc_window", { d: days, n: win }) : t(hass, "not_available"))}</div>
        ${known ? sub : ""}
      </div>`;
    };
    const row = (label, keys, note) => {
      const cells = keys.map(stat).join("");
      if (!cells) return "";
      return `<div class="acc-row"><div class="acc-lab"><div>${label}</div><div class="pvs-sub">${note}</div></div>${cells}</div>`;
    };
    card(`<div class="pvs-head">
        <span class="pvs-title">${esc(cfg.title ?? t(hass, "acc_title"))}</span>
        ${helpChip("accuracy")}
      </div>
      ${row(t(hass, "acc_short"), ACC_KEYS.short, t(hass, "acc_short_note"))}
      ${row(t(hass, "acc_dayahead"), ACC_KEYS.dayahead, t(hass, "acc_dayahead_note"))}`);
  }

  static getConfigElement() { return document.createElement("pvstrings-chain-editor"); }
  static getStubConfig() { return { entities: {} }; }
}

const ACC_CSS = `
  .acc-row { display: grid; grid-template-columns: minmax(110px, 1.2fr) repeat(auto-fit, minmax(96px, 1fr));
    gap: 10px 14px; align-items: end; padding: 10px 0; border-top: 1px solid var(--pvs-hairline); }
  .acc-row:first-of-type { border-top: none; }
  .acc-lab { font-size: 13px; font-weight: 600; align-self: center; }
  .acc-lab .pvs-sub { font-weight: 400; margin-top: 2px; }
  .acc-stat .l { font-size: 11px; color: var(--secondary-text-color); margin-bottom: 2px; white-space: nowrap; }
  .acc-stat .v .pvs-num { font-size: 20px; font-weight: 600; }
  .acc-stat .v .u { font-size: 11px; color: var(--secondary-text-color); margin-left: 3px; }
  .acc-stat .n { font-size: 10.5px; color: var(--secondary-text-color); display: block; margin-top: 1px; }
  .acc-stat.clickable { cursor: pointer; }
`;

/* ======================== SECTION: CARD:KVTABLE ========================== */

// Generic attribute renderer for the nerd view: every table header links to
// its source entity (rule 2 — Markdown tables cannot do that).
/* ======================= SECTION: CARD:HOURPROFILE ======================= */

// Where in the day the day-ahead error sits. The same scored pairs the
// 30-day figure is built from, folded by local hour by the integration
// (>= 1.23) — a daily WMAPE cannot tell a morning that runs hot from an
// afternoon that runs cold, and for anyone sizing a battery reserve for
// their own window that is the entire question.
//
// Two scales, because one would hide the other: the bars carry the energy
// behind an hour, the strip below carries the deviation the reader copies
// into their window.
//
// The percentage is (announced - arrived) / announced, matching the
// integration README's template. That denominator is not a style choice:
// the margin is applied to TOMORROW'S ANNOUNCEMENT, so it has to be a share
// of an announcement. Dividing by what arrived yields a different number
// for the same hour and answers a question nobody asked.
class PvsHourProfileCard extends PvsBaseCard {
  getCardSize() { return 4; }
  getGridOptions() { return { columns: "full", rows: "auto" }; }
  setConfig(config) {
    this._sel = null;          // null = the live 30 days; else a week_start
    this._weeksState = null;
    super.setConfig(config);
  }

  // Past weeks, where the integration keeps them (get_weeks). Same fold, a
  // week at a time: what a month hides — whether the morning got better —
  // becomes a step through the weeks.
  async _ensureWeeks() {
    const hass = this._hass, cfg = this._config;
    if (!cfg?.entity || !hasPvsService(hass, "get_weeks")) return;
    const st = this._weeksState;
    if (st && !(st.at && Date.now() - st.at > 30 * 60000)) return;
    // keep drawing the weeks we have while the refresh runs
    this._weeksState = { ...(st ?? {}), loading: true, at: Date.now() };
    try {
      const m = await getRegistryModel(hass);
      const info = m.byEntityId.get(cfg.entity);
      const node = info?.level === "plant" ? info.node : info?.node?.plant;
      if (!node?.entryId) { this._weeksState = { none: true, at: Date.now() }; return; }
      const data = await historyWeeks(hass, node.entryId);
      const weeks = (data?.weeks ?? []).filter((w) => w && typeof w.week_start === "string"
        && Array.isArray(w.hourly_profile))
        .sort((x, y) => (x.week_start < y.week_start ? -1 : 1));
      this._weeksState = { weeks, at: Date.now() };
    } catch (e) {
      this._weeksState = { problem: esc(e?.message ?? String(e)), at: Date.now() };
    }
    this._render();
  }
  _selWeek() {
    const ws = this._weeksState?.weeks;
    return ws && this._sel != null ? ws.find((w) => w.week_start === this._sel) ?? null : null;
  }
  _stepHTML() {
    const ws = this._weeksState?.weeks, hass = this._hass;
    if (!ws?.length) return "";
    const i = this._sel == null ? ws.length : ws.findIndex((w) => w.week_start === this._sel);
    const label = this._sel == null ? t(hass, "hp_sel_live") : weekLabel(hass, this._sel);
    return stepperHTML(hass, label, { prev: i > 0, next: this._sel != null, past: this._sel != null });
  }
  _step(dir) {
    const ws = this._weeksState?.weeks;
    if (!ws?.length) return;
    const i = (this._sel == null ? ws.length : ws.findIndex((w) => w.week_start === this._sel)) + dir;
    this._sel = i >= ws.length ? null : ws[Math.max(0, i)].week_start;
    this._render();
  }

  _render() {
    const hass = this._hass, cfg = this._config;
    if (!hass || !cfg) return;
    const card = (inner) => {
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${FC_CSS}${HP_CSS}</style><ha-card>${inner}<div class="pvs-tip"></div></ha-card>`;
      this._wire();
    };
    if (!cfg.entity) return card(problemHTML(hass, { reason: t(hass, "no_entity_config") }));
    const st = hass.states[cfg.entity];
    if (!st) return card(problemHTML(hass, { reason: t(hass, "entity_missing", { entity: cfg.entity }) }));
    const need = requireFeatures(st, ["hourly_profile"]);
    if (!need.ok) return card(problemHTML(hass, { entity: cfg.entity, missing: need.missing }));

    this._ensureWeeks();
    const week = this._selWeek();
    // a week is drawn exactly like the live month — only the rows differ
    const a = week
      ? { hourly_profile: week.hourly_profile, days_scored: week.days_scored ?? null }
      : st.attributes;
    const title = cfg.title ?? t(hass, "hp_title");
    const weekChips = !week ? "" : [
      week.complete === false ? `<span class="pvs-chip">${t(hass, "week_running")}</span>` : "",
      week.backfilled ? `<span class="pvs-chip" title="${esc(t(hass, "week_backfilled_tip"))}">${t(hass, "week_backfilled")}</span>` : "",
    ].join("");
    const head = (extraChip = "") => `<div class="pvs-head">
      <span class="pvs-title clickable" data-more-info="${cfg.entity}">${esc(title)}</span>
      ${a.days_scored != null ? `<span class="pvs-chip${week ? "" : " clickable"}"${week ? "" : ` data-more-info="${cfg.entity}"`}>${tn(hass, "hp_days_scored", a.days_scored)}</span>` : ""}
      ${weekChips}${extraChip}${this._stepHTML()}${helpChip("hp")}
    </div>`;

    // Rows worth drawing. Two kinds get dropped, for two different reasons:
    // a 0/0 night row is an aged leftover from before the integration's
    // night fix and measures nothing; everything else stays, including a
    // dawn hour with no announcement and a trace of yield — that one has a
    // pair but no percentage, which the strip handles below.
    const rows = (a.hourly_profile ?? [])
      .filter((r) => r && Number.isFinite(r.hour)
        && ((r.forecast_kwh ?? 0) > 0 || (r.actual_kwh ?? 0) > 0))
      .sort((x, y) => x.hour - y.hour);
    // The accuracy state can still be unknown while the profile has rows —
    // the profile is not gated on the three-day threshold, so the drawing
    // must not be gated on the state either.
    if (!rows.length) return card(head() + withheldHTML(t(hass, "hp_no_hours")));

    const n = rows.length;
    // PAD_T leaves the unit its own line above the top gridline; anchored
    // left, because "kWh/Tag" is wider than the axis gutter and would be cut
    // off hanging off the end of it.
    const PAD_L = 36, PAD_R = 8, PAD_T = 18, PH = 104;
    // The SVG is stretched to the card's width, which scales the text with
    // it — a five-hour profile in a narrow world would render its labels
    // twice the size of an eighteen-hour one. Widen the slots instead.
    const SW = Math.max(26, Math.floor((420 - PAD_L - PAD_R) / n));
    const GAP = 16, STRIP_H = 56, PAD_B = 18;
    const W = PAD_L + n * SW + PAD_R;
    const STRIP_Y = PAD_T + PH + GAP;
    const MID = STRIP_Y + STRIP_H / 2;
    const H = STRIP_Y + STRIP_H + PAD_B;

    // Per scored day, not as the sum the attribute carries. The sums are
    // right but unreadable: an hour of a 30-day window stands four times as
    // tall as the same hour of a week, so stepping through the weeks — the
    // whole point of the stepper — compares nothing. Divided by each row's
    // *own* days, because a dawn hour is scored on fewer days than noon: a
    // flat divide by the window would sink exactly the edges people read.
    const perDay = (v, r) => ((r.days ?? 0) > 0 ? (v ?? 0) / r.days : null);
    let peak = 0;
    for (const r of rows) {
      peak = Math.max(peak, perDay(r.forecast_kwh, r) ?? 0, perDay(r.actual_kwh, r) ?? 0);
    }
    const yMax = niceMax(peak * 1.05);
    const yOf = (v) => PAD_T + PH - (v / yMax) * PH;

    // The gap, in the same unit as the bars above: what the hour promised
    // minus what it delivered, per scored day. Energy, not percent, because
    // the question the card answers is where in the day the forecast leaves
    // energy on the table — and because a percentage lets an hour carrying
    // 0.02 kWh set the scale and flatten noon into a hairline. The share is
    // still in the tooltip, where a model question can find it.
    const gapOf = (r) => {
      const f = perDay(r.forecast_kwh, r), ac = perDay(r.actual_kwh, r);
      return f == null || ac == null ? null : f - ac;
    };
    const shareOf = (r) => (r.forecast_kwh ?? 0) > 0
      ? ((r.forecast_kwh - (r.actual_kwh ?? 0)) / r.forecast_kwh) * 100 : null;
    const gaps = rows.map(gapOf).filter((d) => d != null).map(Math.abs);
    // Symmetric, so "too high" and "too low" stay comparable at a glance;
    // floored against the day's own size so a near-perfect month does not
    // magnify its own noise into a mountain range.
    const gapMax = niceMax(Math.max(peak * 0.1, ...gaps));
    const dOf = (d) => MID - (d / gapMax) * (STRIP_H / 2);

    // Dimming only reads as dimming next to something bright. On a fresh
    // plant every hour is thin, so the chip says it in words — otherwise
    // "thin" and "normal" would look identical (design rule 1).
    const thinCount = rows.filter((r) => (r.days ?? 0) < 3).length;

    const bw = (SW - 7) / 2;
    let bars = "", strip = "", hits = "", labels = "";
    rows.forEach((r, i) => {
      const x0 = PAD_L + i * SW;
      // A thin hour is dimmed, never dropped: "not much evidence" and "no
      // evidence" must not look alike (design rule 1).
      const thin = (r.days ?? 0) < 3;
      const op = thin ? ' opacity="0.5"' : "";
      if (n <= 14 || i % 2 === 0) {
        labels += `<text class="axis" x="${x0 + SW / 2}" y="${H - 5}" text-anchor="middle">${String(r.hour).padStart(2, "0")}</text>`;
      }
      // The deviation strip keeps reading the sums: a ratio divides the day
      // count away, so normalising it would change nothing.
      const f = perDay(r.forecast_kwh, r) ?? 0, ac = perDay(r.actual_kwh, r) ?? 0;
      if (f > 0) {
        bars += `<rect x="${x0 + 2}" y="${yOf(f)}" width="${bw}" height="${PAD_T + PH - yOf(f)}"
          fill="var(--pvs-model)" rx="1"${op}/>`;
      }
      if (ac > 0) {
        bars += `<rect x="${x0 + 3 + bw}" y="${yOf(ac)}" width="${bw}" height="${PAD_T + PH - yOf(ac)}"
          fill="var(--pvs-measure)" rx="1"${op}/>`;
      }
      const dev = gapOf(r);
      if (dev != null) {
        // Colour carries the direction in the palette the whole dashboard
        // reads by: blue is the model, so a bar above the line is the model
        // having promised too much; orange is measurement, so a bar below
        // is reality having beaten it.
        const y = dOf(Math.max(-gapMax, Math.min(gapMax, dev)));
        strip += `<rect x="${x0 + 2}" y="${Math.min(MID, y)}" width="${SW - 4}"
          height="${Math.max(1, Math.abs(MID - y))}"
          fill="${dev >= 0 ? "var(--pvs-model)" : "var(--pvs-measure)"}" opacity="${thin ? 0.3 : 0.7}" rx="1"/>`;
      }
      const tip = { h: r.hour, f, a: ac, dev, share: shareOf(r),
        days: r.days ?? null, thin,
        fsum: r.forecast_kwh ?? 0, asum: r.actual_kwh ?? 0 };
      hits += `<rect class="hit" x="${x0}" y="${PAD_T}" width="${SW}" height="${H - PAD_T - PAD_B}"
        fill="transparent" data-hp='${esc(JSON.stringify(tip))}'/>`;
    });

    let grid = "";
    for (const v of [0, yMax / 2, yMax]) {
      grid += `<line class="grid" x1="${PAD_L}" y1="${yOf(v)}" x2="${W - PAD_R}" y2="${yOf(v)}"/>
        <text class="axis" x="${PAD_L - 5}" y="${yOf(v) + 3}" text-anchor="end">${fmtNum(hass, v, 1)}</text>`;
    }
    grid += `<text class="axis" x="2" y="${PAD_T - 7}" style="font-size:8.5px">${t(hass, "hp_unit")}</text>`;
    grid += `<line class="grid" x1="${PAD_L}" y1="${MID}" x2="${W - PAD_R}" y2="${MID}"/>
      <text class="axis" x="${PAD_L - 5}" y="${STRIP_Y + 4}" text-anchor="end">+${fmtNum(hass, gapMax, 2)}</text>
      <text class="axis" x="${PAD_L - 5}" y="${MID + 3}" text-anchor="end">0</text>
      <text class="axis" x="${PAD_L - 5}" y="${STRIP_Y + STRIP_H + 3}" text-anchor="end">−${fmtNum(hass, gapMax, 2)}</text>`;

    card(`${head(thinCount
      ? `<span class="pvs-chip">${tn(hass, "hp_thin_hours", thinCount)}</span>` : "")}
      <div class="fc-wrap"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="aspect-ratio:${W}/${H}">
        ${grid}${bars}${strip}${labels}${hits}
      </svg></div>
      <div class="pvs-legend">
        <span class="it"><span class="sw" style="background:var(--pvs-model)"></span>${t(hass, "hp_forecast")}</span>
        <span class="it"><span class="sw" style="background:var(--pvs-measure)"></span>${t(hass, "hp_actual")}</span>
        <span class="it" style="margin-left:4px">${t(hass, "hp_gap")}:</span>
        <span class="it"><span class="sw" style="background:var(--pvs-model);opacity:0.7"></span>${t(hass, "hp_too_high")}</span>
        <span class="it"><span class="sw" style="background:var(--pvs-measure);opacity:0.7"></span>${t(hass, "hp_too_low")}</span>
      </div>
      <div class="fc-note">${t(hass, week ? "hp_note_week" : "hp_note")}</div>`);
  }

  _wire() {
    this._wireMoreInfo();
    if (this._wired) return;
    this._wired = true;
    this.shadowRoot.addEventListener("click", (ev) => {
      const b = ev.target.closest?.("[data-step]");
      if (!b || b.disabled) return;
      ev.stopPropagation();
      this._step(parseInt(b.getAttribute("data-step"), 10));
    });
    wireTooltip(this, {
      selector: "[data-hp]",
      content: (el) => {
        const hass = this._hass;
        const c = JSON.parse(el.getAttribute("data-hp"));
        return `<div class="h">${String(c.h).padStart(2, "0")}:00–${String((c.h + 1) % 24).padStart(2, "0")}:00</div>
          <div class="r"><span class="k">${t(hass, "hp_forecast")}</span><span class="v">${fmtKwh(hass, c.f, 2)}</span></div>
          <div class="r"><span class="k">${t(hass, "hp_actual")}</span><span class="v">${fmtKwh(hass, c.a, 2)}</span></div>
          ${c.dev != null ? `<div class="r"><span class="k">${t(hass, "hp_gap")}</span>
            <span class="v">${fmtSigned(hass, c.dev, 2)} kWh</span></div>` : ""}
          ${c.share != null ? `<div class="r"><span class="k">${t(hass, "hp_deviation")}</span>
            <span class="v">${fmtSigned(hass, c.share, 1)} %</span></div>` : ""}
          ${c.days != null ? `<div class="pvs-sub">${t(hass, "hp_sum_of", {
            f: fmtNum(hass, c.fsum, 1), a: fmtNum(hass, c.asum, 1), n: c.days })}</div>` : ""}
          ${c.thin ? `<div class="pvs-sub">${tn(hass, "hp_thin", c.days ?? 0)}</div>` : ""}`;
      },
    });
  }

  static getConfigElement() { return document.createElement("pvstrings-hour-profile-editor"); }
  static getStubConfig(hass, entities) {
    const guess = (entities ?? []).find((e) =>
      Array.isArray(hass.states[e]?.attributes?.hourly_profile));
    return { entity: guess ?? "" };
  }
}

const HP_CSS = `
  .fc-wrap svg { width: 100%; height: auto; display: block; }
`;

const WEATHERS = ["clear", "partly_cloudy", "overcast", "rain"];
const DAYPARTS = ["morning", "midday", "afternoon"];

// Solar noon today at the plant, as a UTC timestamp: longitude plus the
// equation of time (NOAA's short series, good to about a minute). The
// integration keys its hourly buckets relative to solar noon; readers think
// in clock time, so the columns are labelled with the clock time a slot
// starts at today.
function solarNoonMs(lon, now = Date.now()) {
  const d = new Date(now);
  const start = Date.UTC(d.getUTCFullYear(), 0, 0);
  const doy = Math.floor((now - start) / 86400000);
  const g = (2 * Math.PI / 365) * (doy - 1);
  const eot = 229.18 * (0.000075 + 0.001868 * Math.cos(g) - 0.032077 * Math.sin(g)
    - 0.014615 * Math.cos(2 * g) - 0.040849 * Math.sin(2 * g));  // minutes
  const midnight = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
  return midnight + (720 - 4 * lon - eot) * 60000;
}

// The hourly slots a log-ratio table actually holds, in the integration's
// order (daypart_slots), each with its span and the daypart it lies in.
// Null when the integration still learns per daypart -- the caller then draws
// the three-column table exactly as before.
function hourlySlots(attrs, buckets, lat = null) {
  if (attrs?.daypart_scheme !== 2 || !Array.isArray(attrs?.daypart_slots)) return null;
  const present = new Set(Object.keys(buckets ?? {}).map((k) => k.split("|").pop()));
  // The integration keeps slots out to the longest day of the year; today
  // only the ones the sun actually crosses (plus half an hour) are shown, so
  // a September table is not half night. They come back as the days grow.
  const half = lat != null ? halfDayHours(lat) : 12;
  const slots = attrs.daypart_slots.filter((s) =>
    present.has(s.key) && s.to_h > -half - 0.5 && s.from_h < half + 0.5);
  return slots.length ? slots : null;
}

// Half the length of today's day at a latitude, in hours (sunrise equation).
function halfDayHours(lat, now = Date.now()) {
  const d = new Date(now);
  const doy = Math.floor((now - Date.UTC(d.getUTCFullYear(), 0, 0)) / 86400000);
  const decl = -23.44 * Math.cos((2 * Math.PI / 365) * (doy + 10)) * Math.PI / 180;
  const x = -Math.tan(lat * Math.PI / 180) * Math.tan(decl);
  if (x <= -1) return 12;
  if (x >= 1) return 0;
  return Math.acos(x) * 180 / Math.PI / 15;
}

class PvsKvTableCard extends PvsBaseCard {
  getCardSize() { return 3; }
  // Row-based modes (sky_overview, conversion, price) read entities beyond
  // cfg.entity — watch them all, or later rows render stale.
  watchedEntities() {
    const ids = this._config?.entity ? [this._config.entity] : [];
    for (const r of this._config?.rows ?? []) ids.push(r.sky, r.shading, r.out, r.dc, r.entity);
    return ids.filter(Boolean);
  }

  // A learned factor as the percentage it means (+7 % reads, 1.068 must be
  // converted). The raw factor and its evidence sit in the tooltip; n is
  // printed in the cell only while it is thin — on a mature plant 92 copies
  // of "n 21.8" said nothing the maturity bar had not already said.
  _factorCell(hass, cell) {
    if (!cell) return `<td class="miss">${t(hass, "nerd_bucket_missing")}</td>`;
    const pct = (cell.factor - 1) * 100;
    const dev = Math.abs(pct);
    const tone = dev > 15 ? "hot" : dev > 5 ? "warm" : "";
    const thin = (cell.n_eff ?? 0) < MATURITY_MAX_N_EFF * 0.5;
    const showN = thin || this._config?.detail;
    return `<td class="fac ${tone}${thin ? " thin" : ""}" data-fac='${esc(JSON.stringify({ f: cell.factor, n: cell.n_eff ?? null, thin }))}'>
      <span class="pvs-num">${fmtSigned(hass, pct, 1)} %</span>
      ${showN ? `<span class="n">n ${fmtNum(hass, cell.n_eff, 1)}</span>` : ""}</td>`;
  }

  // Hourly factors as a heatmap: one column per slot, grouped under the
  // daypart it replaced, labelled with today's clock time. Same palette as the
  // source-bias heatmap (orange = reality delivered more than computed, blue =
  // less); the number stays small in the cell, evidence in the tooltip.
  _hourlyTable(hass, slots, rows, lead = null) {
    const lon = hass?.config?.longitude;
    const noon = lon != null ? solarNoonMs(lon) : null;
    const CLIP = 30;
    const cell = (c) => {
      if (!c) return `<td class="miss hm-miss"></td>`;
      const pct = (c.factor - 1) * 100;
      const mag = Math.min(1, Math.abs(pct) / CLIP);
      const thin = (c.n_eff ?? 0) < MATURITY_MAX_N_EFF * 0.5;
      const hue = pct < 0 ? "var(--pvs-model)" : "var(--pvs-measure)";
      const mix = Math.round(8 + mag * 72);
      return `<td class="hm${thin ? " thin" : ""}" style="background:color-mix(in srgb, ${hue} ${mix}%, transparent)"
        data-fac='${esc(JSON.stringify({ f: c.factor, n: c.n_eff ?? null, thin }))}'>
        <span class="pvs-num">${Math.abs(pct) < 0.5 ? "0" : fmtSigned(hass, pct, 0)}</span></td>`;
    };
    const groups = [];
    for (const s of slots) {
      const last = groups[groups.length - 1];
      if (last && last.part === s.daypart) last.n += 1;
      else groups.push({ part: s.daypart, n: 1 });
    }
    const label = (s) => noon != null
      ? fmtHour(hass, noon + s.from_h * 3600000)
      : `${s.from_h >= 0 ? "+" : "−"}${Math.abs(s.from_h)}`;
    const leadHead = lead ? `<th>${esc(lead.title)}</th>` : "";
    const leadGap = lead ? `<th></th>` : "";
    return `<div class="hm-scroll"><table class="hm-table hm-hours">
      <tr><th></th>${leadGap}${groups.map((g) => `<th colspan="${g.n}" class="hm-part">${t(hass, "daypart_" + g.part)}</th>`).join("")}</tr>
      <tr><th></th>${leadHead}${slots.map((s) => `<th class="pvs-num hm-clock">${label(s)}</th>`).join("")}</tr>
      ${rows.map((r) => `<tr><th>${esc(r.label)}</th>${lead ? this._factorCell(hass, r.lead) : ""}
        ${slots.map((s) => cell(r.cells[s.key])).join("")}</tr>`).join("")}
    </table></div>
    <div class="hm-legend">
      <span class="lbl">−${CLIP} % ${t(hass, "hourly_legend_less")}</span>
      <span class="ramp"></span>
      <span class="lbl">${t(hass, "hourly_legend_more")} +${CLIP} %</span>
      <span class="thin-sw"><span class="sw"></span>${t(hass, "bias_thin")}</span>
    </div>
    <div class="kv-foot">${t(hass, noon != null ? "hourly_note" : "hourly_note_rel")}</div>`;
  }

  // help_<key> per mode — the "?" in the head replaces the explainer footer
  static helpKeyFor(mode) {
    if (mode.startsWith("log_ratio")) return "factors";
    if (mode === "ghi_bias") return "bias";
    if (mode === "sky_overview") return "sky";
    if (mode === "conversion" || mode === "conversion_evidence") return "conversion";
    if (mode === "collector" || mode === "skip_reasons" || mode === "censoring") return "health";
    return null;
  }

  _wire() {
    this._wireMoreInfo();
    if (this._wired) return;
    this._wired = true;
    wireTooltip(this, {
      selector: "[data-fac]",
      content: (el) => {
        const hass = this._hass;
        const c = JSON.parse(el.getAttribute("data-fac"));
        return `<div class="r"><span class="k">${t(hass, "factor_tip_factor")}</span><span class="v">${fmtNum(hass, c.f, 4)}</span></div>
          ${c.n != null ? `<div class="r"><span class="k">${t(hass, "factor_tip_n")}</span><span class="v">${fmtNum(hass, c.n, 1)}</span></div>` : ""}
          ${c.thin ? `<div class="pvs-sub">${t(hass, "factor_thin")}</div>` : ""}`;
      },
    });
  }

  async _stringNames() {
    // string_id (subentry ULID) -> display name, via device identifiers
    if (this._names) return this._names;
    const hass = this._hass;
    const names = new Map();
    try {
      const devices = await hass.callWS({ type: "config/device_registry/list" });
      for (const d of devices) {
        if (d.model !== "PV string" && d.model !== "Curtailment group") continue;
        for (const ident of d.identifiers ?? []) {
          if (ident[0] === "pvstrings") {
            const parts = String(ident[1]).split("_");
            if (parts.length >= 2) names.set(parts.slice(1).join("_"), d.name_by_user || d.name);
          }
        }
      }
    } catch (_) { /* ids shown raw */ }
    this._names = names;
    this._render();
    return names;
  }

  // Entry diagnostics -> data.conversion_evidence (+ scope display names).
  // Resolved once per card instance; the underlying download is capped at
  // 5 minutes by cachedWS, so state churn does not refetch it.
  async _loadConversionEvidence() {
    if (this._convEv) return;
    this._convEv = "loading";
    let out = { error: "unavailable" };
    try {
      const hass = this._hass;
      const m = await getRegistryModel(hass);
      const info = m.byEntityId.get(this._config.entity);
      const entryId = info?.node?.entryId ?? m.plants[0]?.entryId ?? null;
      const res = await conversionEvidence(hass, entryId);
      // absent block and empty block mean the same thing: no stage is set
      // up to collect (a configured stage always appears, if only with 0/0)
      if (res.evidence && Object.keys(res.evidence).length)
        out = { names: await this._stringNames(), evidence: res.evidence };
      else if (!res.error) out = { error: "none" };
      else out = res;
    } catch (_) { /* stays unavailable -> withheld */ }
    this._convEv = out;
    this._render();
  }

  _render() {
    const hass = this._hass, cfg = this._config;
    if (!hass || !cfg) return;
    const card = (inner) => {
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${KV_CSS}</style><ha-card>${inner}<div class="pvs-tip"></div></ha-card>`;
      this._wire();
    };
    if (!cfg.entity) return card(problemHTML(hass, { reason: t(hass, "no_entity_config") }));
    const st = hass.states[cfg.entity];
    if (!st) return card(problemHTML(hass, { reason: t(hass, "entity_missing", { entity: cfg.entity }) }));
    const a = st.attributes;
    const mode = cfg.mode ?? "generic";
    const title = cfg.title ?? a.friendly_name ?? cfg.entity;
    const helpKey = cfg.help ?? PvsKvTableCard.helpKeyFor(mode);
    const head = `<div class="pvs-head">
      <span class="pvs-title kv-t clickable" data-more-info="${cfg.entity}">${esc(title)}</span>
      ${helpKey ? helpChip(helpKey) : ""}
    </div>`;
    const empty = (path) => card(head + withheldHTML(t(hass, "kv_empty", { path, entity: cfg.entity })));

    let body = "";
    if (mode === "log_ratio_plant") {
      const buckets = a.log_ratio?.plant;
      if (!buckets) return empty("log_ratio.plant");
      const slots = hourlySlots(a, buckets, hass?.config?.latitude);
      if (slots) {
        body = this._hourlyTable(hass, slots, WEATHERS.map((w) => ({
          label: t(hass, "weather_" + w),
          cells: Object.fromEntries(slots.map((s) => [s.key, buckets[`${w}|${s.key}`]])),
        })));
      } else body = `<table><tr><th></th>${DAYPARTS.map((d) => `<th>${t(hass, "daypart_" + d)}</th>`).join("")}</tr>
        ${WEATHERS.map((w) => `<tr><th>${t(hass, "weather_" + w)}</th>
          ${DAYPARTS.map((d) => this._factorCell(hass, buckets[`${w}|${d}`])).join("")}</tr>`).join("")}</table>`;
    } else if (mode === "log_ratio_string_all") {
      // one row per string: its offset beside its three daypart buckets —
      // two tables said the same names twice
      const off = a.log_ratio?.string ?? {}, dp = a.log_ratio?.string_daypart ?? {};
      const ids = [...new Set([...Object.keys(off), ...Object.keys(dp).map((k) => k.split("|")[0])])];
      if (!ids.length) return empty("log_ratio.string");
      const names = this._names ?? (this._stringNames(), new Map());
      const slots = hourlySlots(a, dp, hass?.config?.latitude);
      if (slots) {
        body = this._hourlyTable(hass, slots, ids.map((id) => ({
          label: names.get(id) ?? id.slice(0, 8),
          lead: off[id],
          cells: Object.fromEntries(slots.map((s) => [s.key, dp[`${id}|${s.key}`]])),
        })), { title: t(hass, "col_offset") });
      } else body = `<table><tr><th></th><th>${t(hass, "col_offset")}</th>${DAYPARTS.map((d) => `<th>${t(hass, "daypart_" + d)}</th>`).join("")}</tr>
        ${ids.map((id) => `<tr><th>${esc(names.get(id) ?? id.slice(0, 8))}</th>${this._factorCell(hass, off[id])}
          ${DAYPARTS.map((d) => this._factorCell(hass, dp[`${id}|${d}`])).join("")}</tr>`).join("")}</table>
        <div class="kv-foot">${t(hass, "factor_legend")}</div>`;
    } else if (mode === "log_ratio_strings" || mode === "log_ratio_string_daypart") {
      const src = mode === "log_ratio_strings" ? a.log_ratio?.string : a.log_ratio?.string_daypart;
      if (!src || !Object.keys(src).length) return empty(`log_ratio.${mode === "log_ratio_strings" ? "string" : "string_daypart"}`);
      const names = this._names ?? (this._stringNames(), new Map());
      if (mode === "log_ratio_strings") {
        body = `<table>${Object.entries(src).map(([id, c]) =>
          `<tr><th>${esc(names.get(id) ?? id.slice(0, 8))}</th>${this._factorCell(hass, c)}</tr>`).join("")}</table>`;
      } else {
        const ids = [...new Set(Object.keys(src).map((k) => k.split("|")[0]))];
        body = `<table><tr><th></th>${DAYPARTS.map((d) => `<th>${t(hass, "daypart_" + d)}</th>`).join("")}</tr>
          ${ids.map((id) => `<tr><th>${esc(names.get(id) ?? id.slice(0, 8))}</th>
            ${DAYPARTS.map((d) => this._factorCell(hass, src[`${id}|${d}`])).join("")}</tr>`).join("")}</table>`;
      }
    } else if (mode === "ghi_bias") {
      // Heatmap, not a number grid: sixty "0.912 n 4.0" cells hid the one
      // thing the table exists to show — WHERE in the day and at WHICH
      // horizon the source runs hot or cold. Colour carries the pattern in
      // the palette the whole dashboard reads by (blue = announced too
      // much, orange = too little, as on the hour-profile strip); the
      // number stays in the cell, small; evidence sits in the tooltip and
      // fades the cell while thin.
      const src = a.ghi_bias;
      if (!src || !Object.keys(src).length) return empty("ghi_bias");
      const horizons = ["0-6h", "6-24h", "24-48h", "48h+"];
      const hours = [...new Set(Object.keys(src).map((k) => k.split("|")[0]))].sort();
      const truth = a.truth_source
        ?? hass.states[cfg.truth_entity ?? ""]?.attributes?.truth_source;
      const CLIP = 50;
      const cellOf = (c) => {
        if (!c) return `<td class="miss hm-miss"></td>`;
        const pct = (c.factor - 1) * 100;
        const mag = Math.min(1, Math.abs(pct) / CLIP);
        const thin = (c.n_eff ?? 0) < 3;
        const hue = pct < 0 ? "var(--pvs-model)" : "var(--pvs-measure)";
        const mix = Math.round(8 + mag * 72);
        return `<td class="hm${thin ? " thin" : ""}" style="background:color-mix(in srgb, ${hue} ${mix}%, transparent)"
          data-fac='${esc(JSON.stringify({ f: c.factor, n: c.n_eff ?? null, thin }))}'>
          <span class="pvs-num">${Math.abs(pct) < 0.5 ? "0" : fmtSigned(hass, pct, 0)}</span></td>`;
      };
      body = `${truth ? `<div class="kv-note ${truth === "measured" ? "" : "warn"}">${truth === "measured" ? "✓ " + t(hass, "nerd_truth_measured") : "⚠︎ " + t(hass, "nerd_truth_nowcast")}</div>` : ""}
        <table class="hm-table"><tr><th></th>${horizons.map((h) => `<th>${h}</th>`).join("")}</tr>
        ${hours.map((h) => `<tr><th class="pvs-num">${h}:00</th>
          ${horizons.map((hz) => cellOf(src[`${h}|${hz}`])).join("")}</tr>`).join("")}</table>
        <div class="hm-legend">
          <span class="lbl">−${CLIP} % ${t(hass, "bias_legend_high")}</span>
          <span class="ramp"></span>
          <span class="lbl">${t(hass, "bias_legend_low")} +${CLIP} %</span>
          <span class="thin-sw"><span class="sw"></span>${t(hass, "bias_thin")}</span>
        </div>`;
    } else if (mode === "skip_reasons") {
      const lc = a.last_learn_cycle;
      if (!lc) return empty("last_learn_cycle");
      const skips = lc.skipped_because ?? {};
      const rows = Object.entries(skips).sort((x, y) => y[1] - x[1]);
      body = `<table>
        ${["hours_materialised", "observations_used", "observations_skipped", "censored_hours"]
          .filter((k) => lc[k] !== undefined)
          .map((k) => `<tr><th>${keyLabel(hass, k)}</th><td><span class="pvs-num">${lc[k]}</span></td></tr>`).join("")}
        ${rows.length ? `<tr><th colspan="2" class="kv-sect">${keyLabel(hass, "skipped_because")}</th></tr>` : ""}
        ${rows.map(([k, v]) => `<tr><th class="dim">${esc(k)}</th><td><span class="pvs-num">${v}</span></td></tr>`).join("")}
      </table>`;
    } else if (mode === "censoring") {
      const strings = a.strings;
      if (!strings) return empty("strings");
      body = `<table><tr><th></th><th>${t(hass, "vk_measured")}</th><th>${t(hass, "vk_lower_bound")}</th><th>${t(hass, "vk_reconstructed")}</th><th>${t(hass, "cens_coverage")}</th><th>${t(hass, "cens_curtailed")}</th></tr>
        ${Object.entries(strings).map(([name, s]) => {
          const vk = s.today?.value_kinds ?? {};
          return `<tr><th>${esc(name)}</th>
            <td><span class="pvs-num">${vk.measured ?? 0}</span></td>
            <td class="${(vk.lower_bound ?? 0) > 0 ? "warm" : ""}"><span class="pvs-num">${vk.lower_bound ?? 0}</span></td>
            <td class="${(vk.reconstructed ?? 0) > 0 ? "warm" : ""}"><span class="pvs-num">${vk.reconstructed ?? 0}</span></td>
            <td><span class="pvs-num">${s.today?.coverage_mean != null ? fmtNum(hass, s.today.coverage_mean * 100, 0) + "%" : "—"}</span></td>
            <td><span class="pvs-num">${s.today?.curtailed_fraction != null ? fmtNum(hass, s.today.curtailed_fraction * 100, 0) + "%" : "—"}</span></td></tr>`;
        }).join("")}</table>`;
    } else if (mode === "collector") {
      const keys = ["intervals_written", "events_seen", "write_errors", "watchdog_ticks", "last_flush_duration_ms"];
      body = `<table>
        ${keys.filter((k) => a[k] !== undefined).map((k) =>
          `<tr><th>${keyLabel(hass, k)}</th><td class="${k === "write_errors" && a[k] > 0 ? "hot" : ""}"><span class="pvs-num">${a[k]}</span></td></tr>`).join("")}
        ${a.last_error ? `<tr><th class="dim">last error</th><td class="hot">${esc(a.last_error)}</td></tr>` : ""}
        ${a.weather_ok === false ? `<tr><th class="dim">${t(hass, "health_weather")}</th><td class="hot">${esc(a.weather_error ?? "error")}</td></tr>` : ""}
        ${a.coverage_last ? `<tr><th colspan="2" class="kv-sect">${t(hass, "health_per_string")}</th></tr>` : ""}
        ${Object.entries(a.coverage_last ?? {}).map(([id, v]) =>
          `<tr><th class="dim">${esc((this._names?.get(id)) ?? (this._stringNames(), id.slice(0, 8)))}</th>
           <td class="${v < 0.8 ? "warm" : ""}"><span class="pvs-num">${fmtNum(hass, v * 100, 0)} %</span></td></tr>`).join("")}
      </table>`;
    } else if (mode === "sky_overview") {
      const rows2 = (cfg.rows ?? []).map((r) => {
        const sky = hass.states[r.sky], sh = hass.states[r.shading];
        const sa2 = sky?.attributes;
        const lvl = sa2?.level;
        const fit2 = sa2 ? t(hass, sa2.fit_method === "differential"
          ? "sky_fit_differential" : "sky_fit_absolute") : null;
        const worst = sh?.attributes?.most_shaded?.[0];
        const sector = worst?.sector?.replace("|", "° · ").replace(/-/g, "–") ?? null;
        return `<tr><th class="clickable" data-more-info="${r.sky}">${esc(r.name)}</th>
          <td><span class="pvs-num">${sa2?.cells?.length ?? "—"}</span></td>
          <td>${lvl != null
            ? `<span class="pvs-num">${fmtNum(hass, lvl, 2)}</span> <span class="n">${fit2}</span>`
            : `<span class="n">${fit2 ?? "—"}</span>`}</td>
          <td>${worst ? `<span class="pvs-num" style="white-space:nowrap">${esc(sector)}°</span> <span class="n">${fmtNum(hass, worst.shading_pct, 0)}%</span>` : "—"}</td></tr>`;
      }).join("");
      body = `<table><tr><th></th><th>${t(hass, "sky_cells_col")}</th><th>${t(hass, "sky_level")}</th><th>${t(hass, "sky_worst_col")}</th></tr>${rows2}</table>`;
    } else if (mode === "conversion") {
      // Curves are configured (datasheet/custom) or absent (neutral) — they
      // are not learned, so this table shows configuration + realized ratio,
      // never a training progress. `stages` has no contracted shape yet:
      // render defensively.
      const rows3 = (cfg.rows ?? []).map((r) => {
        const o = hass.states[r.out], d = hass.states[r.dc];
        const oa = o?.attributes ?? {};
        const pathTxt = oa.output_path
          ? t(hass, "conv_out_" + (oa.output_path === "storage" ? "storage" : "direct")) : "—";
        const neutral = oa.curve_source === "neutral";
        const curveTxt = curveLabel(hass, oa.curve_source, oa.conversion_factor, oa.curve_prior) ?? "—";
        const stages = oa.stages;
        const stagesTxt = stages == null ? "—"
          : Array.isArray(stages)
            ? esc(stages.map((s) => stageLabel(hass, s?.name ?? s?.kind ?? (typeof s === "string" ? s : ""))).filter(Boolean).join(" → ") || String(stages.length))
            : `<span class="dim">${esc(JSON.stringify(stages).slice(0, 60))}</span>`;
        const ratio = !neutral && oa.today_kwh != null && d?.attributes?.today_kwh > 0
          ? oa.today_kwh / d.attributes.today_kwh : null;
        return `<tr><th class="clickable" data-more-info="${r.out}">${esc(r.name)}</th>
          <td>${pathTxt}</td>
          <td>${curveTxt}</td>
          <td>${stagesTxt}</td>
          <td>${oa.clipped_kwh > 0 ? `<span class="pvs-num">${fmtNum(hass, oa.clipped_kwh, 2)}</span> kWh` : "—"}</td>
          <td>${neutral ? `<span class="dim">${t(hass, "conv_curve_neutral")}</span>`
            : ratio != null ? `<span class="pvs-num">${fmtNum(hass, ratio * 100, 1)} %</span>` : "—"}</td></tr>`;
      }).join("");
      body = `<table><tr><th></th><th>${t(hass, "conv_path")}</th><th>${t(hass, "conv_curve")}</th><th>${t(hass, "conv_stages")}</th><th>${t(hass, "conv_clipped_col")}</th><th>${t(hass, "conv_today_col")}</th></tr>${rows3}</table>`;
    } else if (mode === "conversion_evidence") {
      // The collector's learning pairs (rows written / usable after the
      // censoring gate), from entry diagnostics. "not yet" and "nothing"
      // must not look alike: usable trailing rows is the gate working; a
      // 0/0 row is configured-but-silent — a real warning.
      const d = this._convEv;
      if (!d || d === "loading") {
        this._loadConversionEvidence();
        body = `<div class="kv-note dim">${t(hass, "conv_ev_loading")}</div>`;
      } else if (d.error === "admin") {
        body = `<div class="kv-note warn">${t(hass, "conv_ev_admin")}</div>`;
      } else if (d.error === "none") {
        // block absent (or empty): nothing is set up to learn from — a known
        // state, and it must not wear the "could not check" message
        body = withheldHTML(t(hass, "conv_ev_none"));
      } else if (d.error) {
        // network/endpoint trouble: unknown state, never claim "nothing"
        body = `<div class="kv-note warn">${t(hass, "conv_ev_unavailable")}</div>`;
      } else {
        const stageTxt = { inverter: t(hass, "conv_stage_inverter"),
          mppt: t(hass, "conv_stage_mppt") };
        const rows4 = Object.entries(d.evidence).map(([k, v]) => {
          const [scope, stage] = k.split("|");
          const name = d.names.get(scope) ?? scope.slice(0, 8);
          // Deliberately no progress bar: a bar needs a target, and until the
          // fit exists there is no "enough" to draw against. usable/rows
          // would only chart the censoring gate catching up — permanently
          // full, and reading like "done" when it means nothing of the kind.
          const silent = v.rows === 0;
          return `<tr>
            <th>${esc(name)}</th>
            <td><span class="n">${stageTxt[stage] ?? esc(stage)}</span></td>
            <td class="${silent ? "hot" : ""}"><span class="pvs-num">${v.usable}</span> / <span class="pvs-num">${v.rows}</span>
              ${silent ? `<span class="n">${t(hass, "conv_ev_silent")}</span>` : ""}</td>
          </tr>`;
        }).join("");
        body = `<table><tr><th></th><th>${t(hass, "conv_ev_stage")}</th>
            <th>${t(hass, "conv_ev_pairs_col")}</th></tr>${rows4}</table>
          <div class="kv-note">${t(hass, "conv_ev_note")}</div>`;
      }
    } else if (mode === "price") {
      // Where the savings figure's inputs came from, per window. Two
      // independent optional blocks share the table because they answer the
      // same question — what the money rests on:
      //   price.*           only where a price sensor recorded something
      //   export_dropped_kwh only where the per-hour export cap bit, which
      //                      has nothing to do with the tariff (a fixed-price
      //                      plant with a battery has it, and no price block)
      // Each set of columns therefore appears on its own evidence.
      const rows5 = (cfg.rows ?? [])
        .map((r) => ({ name: r.name, id: r.entity, a: hass.states[r.entity]?.attributes }))
        .filter((r) => r.a);
      const hasPrice = rows5.some((r) => r.a.price);
      const hasDropped = rows5.some((r) => r.a.export_dropped_kwh != null);
      if (!hasPrice && !hasDropped) return empty("price");
      // The savings sensors carry the site currency; the mean prices are in
      // the same unit, and naming it is half the plausibility check.
      const cur = hass.config?.currency || "EUR";
      const money = (v) => v == null ? "–"
        : `<span class="pvs-num">${fmtNum(hass, v, 4)}</span> <span class="n">${esc(cur)}/kWh</span>`;
      const kwh = (v) => `<span class="pvs-num">${fmtNum(hass, v, 2)}</span> <span class="n">kWh</span>`;
      // An hour can be priced while carrying no energy (a tariff sensor runs
      // through the night), so the split can be empty with a price block
      // present — no percentage rather than a division by zero.
      const share = (v, total) => total > 0
        ? ` <span class="n">${fmtNum(hass, (v / total) * 100, 0)} %</span>` : "";
      const priceCells = (r) => {
        const p = r.a.price;
        // "not priced yet" is a state of its own, not an empty row
        if (!p) return `<td class="miss" colspan="5">${t(hass, "price_none_yet")}</td>`;
        const rec = p.by_basis_kwh?.recorded ?? 0;
        const conf = p.by_basis_kwh?.configured ?? 0;
        const tot = rec + conf;
        return `<td>${kwh(rec)}${share(rec, tot)}</td>
          <td>${kwh(conf)}${share(conf, tot)}</td>
          <td><span class="pvs-num">${p.hours_recorded ?? 0}</span> <span class="n">/ ${p.hours ?? 0}</span></td>
          <td>${money(p.import_per_kwh)}</td>
          <td>${money(p.export_per_kwh)}</td>`;
      };
      const header = `<tr><th></th>
        ${hasPrice ? `<th>${t(hass, "price_recorded")}</th><th>${t(hass, "price_configured")}</th>
          <th>${t(hass, "price_hours")}</th><th>${t(hass, "price_import")}</th>
          <th>${t(hass, "price_export")}</th>` : ""}
        ${hasDropped ? `<th>${t(hass, "price_dropped")}</th>` : ""}</tr>`;
      // Deliberately uncoloured: dropped export is a fact about the meter,
      // not a fault and not a loss.
      const dropCell = (r) => `<td>${r.a.export_dropped_kwh != null
        ? kwh(r.a.export_dropped_kwh) : "–"}</td>`;
      // The caveat belongs to the annual estimate on savings_total. It lives
      // in the card rather than in a markdown card of its own so its source
      // stays one click away (design rule 2). The key is always published,
      // carrying null where it does not apply — test the value, never the
      // presence.
      const caveat = rows5.some((r) => r.a.annual_estimate_caveat === "time_of_use");
      body = `<table>${header}
        ${rows5.map((r) => `<tr><th class="clickable" data-more-info="${r.id}">${esc(r.name)}</th>
          ${hasPrice ? priceCells(r) : ""}${hasDropped ? dropCell(r) : ""}</tr>`).join("")}</table>
        ${hasPrice ? `<div class="kv-note">${t(hass, "price_note")}</div>` : ""}
        ${hasDropped ? `<div class="kv-note">${t(hass, "price_dropped_note")}</div>` : ""}
        ${caveat ? `<div class="kv-note">${t(hass, "price_tou_caveat")}</div>` : ""}`;
    } else {
      // generic: dot-path into attributes -> k/v table
      let obj = a;
      for (const p of (cfg.path ?? "").split(".").filter(Boolean)) obj = obj?.[p];
      if (obj == null || typeof obj !== "object") return empty(cfg.path ?? "(root)");
      body = `<table>${Object.entries(obj).map(([k, v]) =>
        `<tr><th>${esc(k)}</th><td>${typeof v === "object" ? `<span class="dim">${esc(JSON.stringify(v))}</span>` : `<span class="pvs-num">${esc(v)}</span>`}</td></tr>`).join("")}</table>`;
    }
    card(head + `<div class="kv-scroll">${body}</div>`);
  }

  static getConfigElement() { return document.createElement("pvstrings-chain-editor"); }
  static getStubConfig() { return { entity: "", mode: "generic" }; }
}

const KV_CSS = `
  .kv-scroll { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-size: 11.5px; }
  th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--pvs-hairline); }
  th { color: var(--secondary-text-color); font-weight: 500; }
  tr > th:first-child { white-space: nowrap; }
  td { color: var(--primary-text-color); }
  td .n { color: var(--secondary-text-color); font-size: 10px; margin-left: 5px; }
  td.miss { color: var(--secondary-text-color); font-style: italic; opacity: 0.7; }
  td.warm { background: color-mix(in srgb, var(--warning-color, #ffa600) 9%, transparent); }
  td.hot { background: color-mix(in srgb, var(--error-color, #d03b3b) 10%, transparent); }
  .kv-t { font-size: 13px; }
  .kv-t.clickable { cursor: pointer; }
  .kv-sect { padding-top: 10px; font-size: 10px; letter-spacing: 0.6px; text-transform: uppercase; }
  .kv-note { font-size: 11px; padding: 5px 8px; border-radius: 6px; background: var(--pvs-chip-bg); color: var(--secondary-text-color); margin-bottom: 8px; }
  .kv-note.warn { background: color-mix(in srgb, var(--warning-color, #ffa600) 12%, transparent); color: var(--primary-text-color); }
  th.dim, .dim { color: var(--secondary-text-color); font-weight: 400; }
  td.fac { white-space: nowrap; }
  td.fac.thin .pvs-num { opacity: 0.6; }
  .kv-foot { font-size: 10.5px; color: var(--secondary-text-color); margin-top: 8px; line-height: 1.5; }
  /* heatmap: tight cells, number small and quiet, colour does the talking */
  .hm-table td { padding: 3px 6px; text-align: center; border-bottom: 1px solid var(--card-background-color); }
  .hm-table th { font-size: 10.5px; }
  .hm-table td.hm { cursor: crosshair; border-radius: 3px; }
  .hm-table td.hm .pvs-num { font-size: 10.5px; color: var(--primary-text-color); }
  .hm-table td.hm.thin .pvs-num { opacity: 0.45; }
  .hm-table td.hm.thin { background-image: repeating-linear-gradient(45deg, transparent 0 4px, color-mix(in srgb, var(--card-background-color) 60%, transparent) 4px 6px); }
  .hm-miss { background: var(--pvs-unobserved); }
  .hm-scroll { overflow-x: auto; }
  .hm-hours td { padding: 3px 3px; min-width: 26px; }
  .hm-hours th.hm-part { font-weight: 400; color: var(--secondary-text-color); border-bottom: 1px solid var(--divider-color); }
  .hm-hours th.hm-clock { font-weight: 400; font-size: 9.5px; color: var(--secondary-text-color); white-space: nowrap; }
  .hm-legend { display: flex; align-items: center; gap: 8px; margin-top: 10px; font-size: 10.5px; color: var(--secondary-text-color); flex-wrap: wrap; }
  .hm-legend .ramp { flex: 1 1 80px; height: 8px; border-radius: 4px; min-width: 60px;
    background: linear-gradient(90deg, color-mix(in srgb, var(--pvs-model) 80%, transparent), transparent 50%, color-mix(in srgb, var(--pvs-measure) 80%, transparent)); }
  .hm-legend .thin-sw { display: inline-flex; align-items: center; gap: 5px; margin-left: 6px; }
  .hm-legend .thin-sw .sw { width: 12px; height: 8px; border-radius: 2px; background: color-mix(in srgb, var(--pvs-measure) 40%, transparent);
    background-image: repeating-linear-gradient(45deg, transparent 0 3px, color-mix(in srgb, var(--card-background-color) 60%, transparent) 3px 5px); }
`;

// Stage names arrive as the integration's identifiers (inverter_efficiency,
// mppt_efficiency, charge_efficiency); readers get the device name.
function stageLabel(hass, s) {
  const k = String(s ?? "").replace(/_efficiency$/, "");
  if (k === "inverter") return t(hass, "conv_stage_inverter");
  if (k === "mppt") return t(hass, "conv_stage_mppt");
  if (k === "charge") return t(hass, "conv_stage_charge");
  return s;
}

/* ========================= SECTION: CARD:MATURITY ======================== */

// How far along the training is, on two axes with deliberately different
// clocks: weather buckets fill within weeks, the sky map can only fill as
// fast as the calendar moves the sun. One blended number would hide that.
//
// Mirrors learning.py: HALFLIFE = 15 effective observations, so a bucket's
// n_eff converges on 1/ALPHA ≈ 22 and stops there — "full" is a real,
// reachable ceiling, not an asymptote.
const MATURITY_MAX_N_EFF = 1 / (1 - 0.5 ** (1 / 15));

// Weather axis of the maturity: evidence held across all plant buckets,
// against the ceiling. `nEffOf(key)` reads a bucket's n_eff from wherever the
// buckets live — the live model sensor or a week's snapshot.
function weatherMaturity(nEffOf) {
  const total = WEATHERS.length * DAYPARTS.length;
  let sum = 0, seen = 0;
  for (const w of WEATHERS) for (const d of DAYPARTS) {
    const n = nEffOf(`${w}|${d}`);
    if (n > 0) { seen++; sum += Math.min(1, n / MATURITY_MAX_N_EFF); }
  }
  return { pct: (sum / total) * 100, seen, total };
}

class PvsMaturityCard extends PvsBaseCard {
  getCardSize() { return 2; }
  getGridOptions() { return { columns: "full", rows: "auto" }; }
  watchedEntities() {
    return [this._config?.entity, ...(this._config?.rows ?? []).map((r) => r.sky)]
      .filter(Boolean);
  }

  _render() {
    const hass = this._hass, cfg = this._config;
    if (!hass || !cfg) return;

    // weather axis: evidence held across all plant buckets, vs the ceiling
    const buckets = (cfg.entity ? hass.states[cfg.entity] : null)?.attributes?.log_ratio?.plant;
    const weather = buckets ? weatherMaturity((key) => buckets[key]?.n_eff) : null;

    // shading axis: observed pooled sky cells vs the year's sun path
    const lat = hass.config?.latitude;
    const annual = lat != null ? annualSkyCells(lat) : null;
    const perString = (cfg.rows ?? []).map((r) => {
      const sky = hass.states[r.sky];
      if (!sky || !annual) return null;
      const pooled = (sky.attributes?.cells ?? []).filter((c) => c.season == null).length;
      return { name: r.name, id: r.sky, pct: Math.min(100, (pooled / annual) * 100) };
    }).filter(Boolean);
    const shading = perString.length
      ? { pct: perString.reduce((s, x) => s + x.pct, 0) / perString.length } : null;

    // goodAt: the point that in practice counts as fully learned. Weather
    // buckets never average near 100 (forgetting keeps rare buckets low), so
    // the tick shows the real target; a sky map genuinely can fill up.
    const bar = (label, m, sub, moreInfo, goodAt, tick) => {
      const good = m != null && m.pct >= goodAt;
      return `
      <div class="mat-axis">
        <div class="mat-row">
          <span class="mat-label${moreInfo ? " clickable" : ""}"${moreInfo ? ` data-more-info="${moreInfo}"` : ""}>${esc(label)}</span>
          <span class="mat-pct pvs-num${good ? " good" : ""}">${m == null ? "—" : fmtNum(hass, m.pct, 0) + " %"}</span>
        </div>
        <div class="mat-track">
          <div class="mat-fill${good ? " good" : ""}" style="width:${m == null ? 0 : Math.max(1.5, m.pct)}%"></div>
          ${tick ? `<div class="mat-tick" style="left:${goodAt}%"></div>` : ""}
        </div>
        <div class="pvs-sub">${sub}</div>
      </div>`;
    };

    const wSub = weather
      ? t(hass, "maturity_buckets", { seen: weather.seen, total: weather.total })
      : t(hass, "kv_empty", { path: "log_ratio.plant", entity: cfg.entity ?? "model_observations" });
    const sSub = !annual
      ? t(hass, "maturity_no_lat")
      : perString.map((s) =>
          `<span class="mat-chip clickable" data-more-info="${s.id}">${esc(s.name)} · <span class="pvs-num">${fmtNum(hass, s.pct, 0)} %</span></span>`,
        ).join("") || t(hass, "kv_empty", { path: "cells", entity: "sky_map" });

    this.shadowRoot.innerHTML = `<style>${BASE_CSS}${MATURITY_CSS}</style><ha-card>
      <div class="pvs-head mat-head">
        <span class="pvs-title${cfg.entity ? " clickable" : ""}"${cfg.entity ? ` data-more-info="${cfg.entity}"` : ""}>${esc(cfg.title ?? t(hass, "maturity_title"))}</span>
        ${helpChip("maturity")}
      </div>
      <div class="mat-wrap">
        ${bar(t(hass, "maturity_weather"), weather, wSub, cfg.entity, 85, true)}
        ${bar(t(hass, "maturity_shading"), shading, sSub, null, 95, false)}
      </div><div class="pvs-tip"></div></ha-card>`;
    this._wireMoreInfo();
    if (!this._wired) { this._wired = true; wireTooltip(this); }
  }

  static getConfigElement() { return document.createElement("pvstrings-chain-editor"); }
  static getStubConfig() { return { entity: "", rows: [] }; }
}

const MATURITY_CSS = `
  ha-card { padding: 16px; }
  .mat-head { margin-bottom: 2px; }
  .mat-wrap { display: flex; gap: 28px; padding: 6px 0 2px; flex-wrap: wrap; }
  .mat-axis { flex: 1 1 240px; min-width: 0; }
  .mat-row { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 6px; }
  .mat-label { font-size: 13px; font-weight: 500; }
  .mat-label.clickable { cursor: pointer; }
  .mat-pct { font-size: 15px; font-weight: 600; }
  .mat-track { position: relative; height: 6px; border-radius: 3px; background: var(--pvs-unobserved); }
  .mat-fill { height: 100%; border-radius: 3px; background: var(--pvs-model); }
  .mat-fill.good { background: var(--success-color, #43a047); }
  .mat-pct.good { color: var(--success-color, #43a047); }
  .mat-tick { position: absolute; top: -3px; bottom: -3px; width: 2px; border-radius: 1px;
    background: color-mix(in srgb, var(--primary-text-color, #212121) 40%, transparent); }
  .mat-axis .pvs-sub { margin-top: 7px; line-height: 1.7; }
  .mat-chip { display: inline-block; margin-right: 10px; cursor: pointer; }
`;

/* ========================= SECTION: CARD:LEARNING ======================== */

// Whether learning pays, week by week, and in total. Two numbers people
// actually ask about, drawn so that neither can be dressed up:
//
// - The main line is the forecast error the learning avoided, in kWh,
//   against the same integration with learning switched off (get_weeks
//   `baseline`: same code, geometry and weather run, apply_learning=False —
//   not "bare physics", which already carries the learned shading map).
//   It is a running sum from the first week compared live. A sum of
//   mostly positive weeks rises by itself; a bad week bends it and stays
//   visible; a plant where learning does nothing draws a flat line, which is
//   a finding, not a failure of the card.
// - The thin second line is how much the model has seen (the maturity
//   formula). It only ever grows, and its label says "seen", not "better".
//
// What it does not do: pick a window, smooth a week away, or start the axis
// anywhere but zero. Weeks rebuilt after the fact are hatched and kept out of
// the sum (named with their own total below the chart), the running week is
// faint, and a week without a baseline is marked, not skipped.
class PvsLearningCard extends PvsBaseCard {
  getCardSize() { return 5; }
  getGridOptions() { return { columns: "full", rows: "auto" }; }
  setConfig(config) {
    this._state = null;
    super.setConfig(config);
  }
  // service data: re-read once the half-hour cache has run out, so the
  // running week does not freeze at whatever it said when the card was made
  _shouldUpdate(prev) {
    return !prev || (!!this._state?.at && Date.now() - this._state.at > 30 * 60000);
  }

  async _load() {
    const hass = this._hass, cfg = this._config;
    const had = this._state?.weeks ? this._state : null;
    this._state = had ? { ...had, at: Date.now() } : { loading: true, at: Date.now() };
    try {
      const m = await getRegistryModel(hass);
      const info = m.byEntityId.get(cfg.entity);
      const node = info?.level === "plant" ? info.node : info?.node?.plant;
      if (!node?.entryId) {
        this._state = { at: Date.now(), problem: t(hass, "entity_missing", { entity: esc(cfg.entity) }) };
      } else {
        const data = await historyWeeks(hass, node.entryId);
        this._state = { at: Date.now(), weeks: data.weeks
          .filter((w) => w && typeof w.week_start === "string")
          .sort((x, y) => (x.week_start < y.week_start ? -1 : 1)) };
      }
    } catch (e) {
      this._state = had ?? { at: Date.now(), problem: t(hass, "hist_failed", { error: esc(e?.message ?? String(e)) }) };
    }
    this._render();
  }

  _render() {
    const hass = this._hass, cfg = this._config;
    if (!hass || !cfg) return;
    const card = (inner) => {
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${FC_CSS}${LEARN_CSS}</style><ha-card>${inner}<div class="pvs-tip"></div></ha-card>`;
      this._wire();
    };
    const title = cfg.title ?? t(hass, "learn_title");
    const head = (chips = "") => `<div class="pvs-head"><span class="pvs-title">${esc(title)}</span>${chips}${helpChip("learning")}</div>`;
    if (!cfg.entity) return card(problemHTML(hass, { reason: t(hass, "no_entity_config") }));
    if (!hasPvsService(hass, "get_weeks")) {
      return card(head() + problemHTML(hass, { reason: t(hass, "hist_needs_service", { service: "pvstrings.get_weeks" }) }));
    }
    if (!this._state || (this._state.at && Date.now() - this._state.at > 30 * 60000)) this._load();
    if (!this._state || this._state.loading) return card(`${head()}<div class="pvs-sub">…</div>`);
    if (this._state.problem) return card(head() + problemHTML(hass, { reason: this._state.problem }));

    const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
    // The error summed per hour, never per day. A day's morning and
    // afternoon errors cancel in a daily net, and a forecast without a
    // multiplicative correction cancels that way more readily than one with
    // it — the daily figure would hand the baseline a win it did not earn.
    // A week that carries only the daily figure (written before PV Strings
    // 1.25.2) therefore draws no bar: mixing the two on one axis would make
    // weeks look comparable that are not.
    const errOf = (b) => num(b?.abs_error_hourly_kwh);
    const rows = this._state.weeks.map((w) => {
      const errWith = errOf(w.day_ahead), errBase = errOf(w.baseline);
      // Where the week's baseline came from decides whether it may count.
      // `live` is the only basis that compares this code against itself;
      // rebuilt and mixed weeks also compare two versions of the code, and
      // an older integration cannot say at all. An integration without the
      // field falls back to its one flag.
      const basis = w.baseline_basis
        ?? (w.baseline == null ? null : w.backfilled ? "replayed" : "live");
      const complete = w.complete !== false;
      return {
        week: w.week_start, complete, basis,
        days: num(w.day_ahead?.days) ?? num(w.days_scored),
        weekDays: num(w.days_scored),
        hours: num(w.day_ahead?.hours),
        gain: errWith != null && errBase != null ? errBase - errWith : null,
        pct: errWith != null && num(w.day_ahead?.actual_kwh) > 0
          ? (errWith / w.day_ahead.actual_kwh) * 100 : null,
        pctBase: errBase != null && num(w.baseline?.actual_kwh) > 0
          ? (errBase / w.baseline.actual_kwh) * 100 : null,
        counts: false,
      };
    });
    for (const r of rows) r.counts = r.gain != null && r.complete && r.basis === "live";
    const counted = rows.filter((r) => r.counts);
    const shown = rows.filter((r) => r.gain != null);
    if (!shown.length) {
      const anyOld = rows.length && this._state.weeks.some((w) => w.baseline != null);
      return card(head() + withheldHTML(t(hass,
        !rows.length ? "learn_no_weeks" : anyOld ? "learn_needs_hourly" : "learn_no_baseline")));
    }

    // ---- hero: one sentence, and only when it can be said ----------------
    // Under three counted weeks the card says what it is waiting for instead
    // of naming a figure. A single week is a weather sample, not a verdict,
    // and it would be read as one.
    const MIN_WEEKS = 3;
    let left;
    if (counted.length >= MIN_WEEKS) {
      const total = counted.reduce((a, r) => a + r.gain, 0);
      const key = Math.abs(total) < 0.5 ? "learn_hero_equal"
        : total > 0 ? "learn_hero_less" : "learn_hero_more";
      left = `<span class="hv" style="color:var(--pvs-model)">${fmtNum(hass, Math.abs(total), 1)}<span class="hu">kWh</span></span>
        <span class="hl">${tn(hass, key, counted.length)}</span>`;
    } else {
      left = `<span class="hl">${t(hass, "learn_hero_thin", { have: counted.length, need: MIN_WEEKS })}</span>`;
    }
    const last = [...rows].reverse().find((r) => r.complete && r.pct != null);
    const hero = `<div class="lr-hero">
      <div class="fc-hero-item">${left}</div>
      ${last ? `<div class="fc-hero-item right">
        <span class="hv" style="color:var(--primary-text-color)">${fmtNum(hass, last.pct, 1)}<span class="hu">%</span></span>
        <span class="hl">${last.basis === "live" && last.pctBase != null
          ? t(hass, "learn_hero_wmape", { base: fmtNum(hass, last.pctBase, 1), week: isoWeekOf(last.week) })
          : t(hass, "learn_hero_wmape_only", { week: isoWeekOf(last.week) })}</span>
      </div>` : ""}
    </div>`;

    // ---- one chart: the week's gain, nothing layered on top --------------
    const n = rows.length;
    // PAD_T leaves the unit a line of its own above the first tick.
    const PAD_L = 40, PAD_R = 10, PAD_T = 20, STRIP_H = 84, PAD_B = 18;
    const SW = Math.max(26, Math.floor((560 - PAD_L - PAD_R) / n));
    const W = PAD_L + n * SW + PAD_R;
    const MID = PAD_T + STRIP_H / 2;
    const H = PAD_T + STRIP_H + PAD_B;
    const gMax = niceMax(Math.max(...shown.map((r) => Math.abs(r.gain))) || 1);
    const gOf = (g) => MID - (g / gMax) * (STRIP_H / 2);

    let grid = `<line class="grid" x1="${PAD_L}" y1="${MID}" x2="${W - PAD_R}" y2="${MID}"
        style="stroke:var(--secondary-text-color);opacity:.35"/>
      <text class="axis" x="${PAD_L - 5}" y="${PAD_T + 4}" text-anchor="end">+${fmtNum(hass, gMax, 1)}</text>
      <text class="axis" x="${PAD_L - 5}" y="${MID + 3}" text-anchor="end">0</text>
      <text class="axis" x="${PAD_L - 5}" y="${PAD_T + STRIP_H + 3}" text-anchor="end">−${fmtNum(hass, gMax, 1)}</text>
      <text class="axis" x="2" y="${PAD_T - 9}" style="font-size:8.5px">kWh</text>`;

    let bars = "", hits = "", labels = "";
    const labelEvery = Math.ceil(n / 12);
    rows.forEach((r, i) => {
      const x0 = PAD_L + i * SW, x = x0 + SW / 2;
      if (r.gain != null) {
        const y = gOf(Math.max(-gMax, Math.min(gMax, r.gain)));
        const bh = Math.max(1, Math.abs(MID - y));
        // A week that does not count is drawn faintly and hatched: it has a
        // figure, but not one this card may add up.
        bars += `<rect x="${x0 + 3}" y="${Math.min(MID, y)}" width="${SW - 6}" height="${bh}" rx="1"
          fill="${r.gain >= 0 ? "var(--success-color, #43a047)" : "var(--pvs-measure)"}" opacity="${r.counts ? 0.8 : 0.25}"/>`;
        if (!r.counts) {
          bars += `<rect x="${x0 + 3}" y="${Math.min(MID, y)}" width="${SW - 6}" height="${bh}" fill="url(#pvs-hatch-lr)"/>`;
        }
      } else {
        // no comparison for this week: a stub, never a zero bar
        bars += `<rect x="${x0 + 3}" y="${MID - 3}" width="${SW - 6}" height="6" rx="1" fill="url(#pvs-hatch-lr)"/>`;
      }
      if (i % labelEvery === (n - 1) % labelEvery) {
        labels += `<text class="axis" x="${x}" y="${H - 5}" text-anchor="middle">${isoWeekOf(r.week)}</text>`;
      }
      hits += `<rect class="hit" x="${x0}" y="${PAD_T}" width="${SW}" height="${STRIP_H}"
        fill="transparent" data-lw='${esc(JSON.stringify(r))}'/>`;
    });

    const excluded = shown.filter((r) => !r.counts);
    card(`${head()}
      ${hero}
      <div class="fc-wrap"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="aspect-ratio:${W}/${H}">
        <defs>${hatchPattern("pvs-hatch-lr")}</defs>
        ${grid}${bars}${labels}${hits}
      </svg></div>
      <div class="pvs-legend">
        <span class="it"><span class="sw" style="background:var(--success-color, #43a047);opacity:.8"></span>${t(hass, "learn_leg_gain")}</span>
        <span class="it"><span class="sw" style="background:var(--pvs-measure);opacity:.8"></span>${t(hass, "learn_leg_loss")}</span>
        ${excluded.length ? `<span class="it"><svg width="14" height="10"><rect width="14" height="10" rx="2" fill="url(#pvs-hatch-lrl)"/><defs>${hatchPattern("pvs-hatch-lrl")}</defs></svg>${t(hass, "learn_leg_excluded")}</span>` : ""}
      </div>
      <div class="fc-note">${t(hass, "learn_note")}</div>`);
  }

  _wire() {
    this._wireMoreInfo();
    if (this._wired) return;
    this._wired = true;
    wireTooltip(this, {
      selector: "[data-lw]",
      content: (el) => {
        const hass = this._hass;
        const r = JSON.parse(el.getAttribute("data-lw"));
        const row = (k, v) => `<div class="r"><span class="k">${k}</span><span class="v">${v}</span></div>`;
        return `<div class="h">${esc(weekLabel(hass, r.week))}</div>
          ${r.pct != null ? row(t(hass, "learn_tip_with"), `${fmtNum(hass, r.pct, 1)} %`) : ""}
          ${r.pctBase != null ? row(t(hass, "learn_tip_without"), `${fmtNum(hass, r.pctBase, 1)} %`) : ""}
          ${r.gain != null ? row(t(hass, "learn_tip_gain"), `${fmtSigned(hass, r.gain, 1)} kWh`) : ""}
          ${r.days != null ? row(t(hass, "learn_tip_days"), r.days) : ""}
          ${r.gain == null ? `<div class="pvs-sub">${t(hass, "learn_tip_no_baseline")}</div>`
            : !r.complete ? `<div class="pvs-sub">${t(hass, "learn_tip_running")}</div>`
            : r.basis && r.basis !== "live" ? `<div class="pvs-sub">${t(hass, `learn_tip_${r.basis}`)}</div>` : ""}`;
      },
    });
  }

  static getConfigElement() { return document.createElement("pvstrings-hour-profile-editor"); }
  static getStubConfig(hass, entities) {
    const guess = (entities ?? []).find((e) =>
      Array.isArray(hass.states[e]?.attributes?.hourly_profile));
    return { entity: guess ?? "" };
  }
}

const LEARN_CSS = `
  .lr-hero { display: flex; justify-content: space-between; align-items: flex-start; gap: 14px; margin: 2px 0 8px; flex-wrap: wrap; }
  .lr-hero .fc-hero-item .hl { max-width: 360px; }
  .hit { cursor: crosshair; }
`;

/* ========================== SECTION: CARD:HEALTH ========================= */

// Collection and learn cycle as one row of chips. Four tables used to say
// "healthy" in thirty numbers; here every figure is a chip that links to its
// source entity, and a table opens only where a figure deviates (or always,
// in detail mode). Rule 1 holds because a chip states the number, never a
// verdict: "0 skipped" and "no learn cycle yet" are different chips.
class PvsHealthCard extends PvsBaseCard {
  getCardSize() { return 2; }
  getGridOptions() { return { columns: "full", rows: "auto" }; }
  watchedEntities() {
    const c = this._config ?? {};
    return [c.entity, c.model_entity, c.detail_entity, c.ghi_entity].filter(Boolean);
  }

  _render() {
    const hass = this._hass, cfg = this._config;
    if (!hass || !cfg) return;
    const card = (inner) => {
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${KV_CSS}${HEALTH_CSS}</style><ha-card>${inner}<div class="pvs-tip"></div></ha-card>`;
      this._wireMoreInfo();
      if (!this._wired) { this._wired = true; wireTooltip(this); }
    };
    if (!cfg.entity) return card(problemHTML(hass, { reason: t(hass, "no_entity_config") }));
    const coll = hass.states[cfg.entity];
    if (!coll) return card(problemHTML(hass, { reason: t(hass, "entity_missing", { entity: cfg.entity }) }));
    const need = requireFeatures(coll, ["collector"]);
    if (!need.ok) return card(problemHTML(hass, { entity: cfg.entity, missing: need.missing }));

    const a = coll.attributes;
    const detail = !!cfg.detail;
    const lc = cfg.model_entity ? hass.states[cfg.model_entity]?.attributes?.last_learn_cycle ?? null : null;
    // skips minus night hours: the ones that say something about the plant
    const night = lc?.skipped_because?.night ?? 0;
    const skipped = Math.max(0, (lc?.observations_skipped ?? 0) - night);
    const sd = cfg.detail_entity ? hass.states[cfg.detail_entity]?.attributes?.strings ?? null : null;
    const ghi = cfg.ghi_entity ? hass.states[cfg.ghi_entity]?.attributes ?? null : null;
    if (!this._names) {
      stringNamesById(hass).then((m) => { this._names = m; this._render(); }).catch(() => { this._names = new Map(); });
    }
    const names = this._names ?? new Map();
    const nameOf = (id) => names.get(id) ?? id.slice(0, 8);

    const chip = (label, value, { tone = "", mi = null, title = null } = {}) =>
      `<span class="pvs-chip hc ${tone}${mi ? " clickable" : ""}"${mi ? ` data-more-info="${mi}"` : ""}${title ? ` title="${esc(title)}"` : ""}>
        <span class="k">${label}</span><span class="v">${value}</span></span>`;

    // ---- chips --------------------------------------------------------------
    const cov = Number.isFinite(parseFloat(coll.state)) ? parseFloat(coll.state) : null;
    const chips = [];
    chips.push(chip(t(hass, "health_coverage"), cov == null ? "—" : `${fmtNum(hass, cov, 0)} %`,
      { tone: cov != null && cov < 80 ? "warn" : "", mi: cfg.entity }));
    const werr = a.write_errors ?? 0;
    chips.push(chip(t(hass, "health_write_errors"), String(werr), { tone: werr > 0 ? "hot" : "", mi: cfg.entity }));
    chips.push(a.weather_ok === false
      ? chip(t(hass, "health_weather"), esc(a.weather_error ?? "error"), { tone: "hot", mi: cfg.entity })
      : chip(t(hass, "health_weather"), t(hass, "health_ok"), { mi: cfg.entity }));
    if (cfg.model_entity) {
      if (lc) {
        // Night is not a skip worth reporting: there is nothing to learn from
        // an hour without sun, and a cycle early in the morning sees nothing
        // else. Counting it made every such cycle look broken ("0 of 5,
        // skipped 5"). Only the daylight hours are measured against.
        const hours = Math.max(0, (lc.hours_materialised ?? 0) - night);
        chips.push(night > 0 && hours === 0
          ? chip(t(hass, "health_learn"), t(hass, "health_learn_night"),
              { tone: "off", mi: cfg.model_entity, title: t(hass, "health_learn_night_tip") })
          // a cycle that found no new hour is neither healthy nor a fault —
          // grey, and the number says it
          : chip(t(hass, "health_learn"),
              t(hass, "health_learn_v", { used: lc.observations_used ?? 0, hours }),
              { tone: skipped > 0 ? "warn" : hours === 0 ? "off" : "", mi: cfg.model_entity }));
        if (skipped > 0) chips.push(chip(t(hass, "health_skipped"), String(skipped), { tone: "warn", mi: cfg.model_entity }));
        const cens = lc.censored_hours ?? 0;
        chips.push(chip(t(hass, "health_censored"), String(cens), { tone: cens > 0 ? "warn" : "", mi: cfg.model_entity }));
      } else {
        chips.push(chip(t(hass, "health_learn"), t(hass, "health_no_cycle"), { tone: "off", mi: cfg.model_entity }));
      }
    }
    // censoring split from strings_detail: lower bounds and reconstructed
    // intervals are the two kinds of "not quite measured"
    let lower = 0, recon = 0, curtailed = false, intervals = [], covMeans = [];
    for (const s of Object.values(sd ?? {})) {
      const td = s?.today ?? {};
      lower += td.value_kinds?.lower_bound ?? 0;
      recon += td.value_kinds?.reconstructed ?? 0;
      if ((td.curtailed_fraction ?? 0) > 0) curtailed = true;
      if (td.intervals != null) intervals.push(td.intervals);
      if (td.coverage_mean != null) covMeans.push(td.coverage_mean);
    }
    if (sd && (lower > 0 || recon > 0)) {
      chips.push(chip(`${t(hass, "vk_lower_bound")} · ${t(hass, "vk_reconstructed")}`,
        `${lower} · ${recon}`, { tone: "warn", mi: cfg.detail_entity }));
    }
    // station air (>= 1.24): the check that measured air reaches the physics
    if (ghi && "station_air_share_today" in ghi) {
      const s = ghi.station_air_share_today;
      const has = s && (s.temperature != null || s.wind != null);
      const pct = (v) => v == null ? "—" : fmtNum(hass, v * 100, 0);
      chips.push(has
        ? chip(t(hass, "health_station"), t(hass, "health_station_v", { t: pct(s.temperature), w: pct(s.wind) }),
            { tone: (s.temperature ?? 1) < 0.8 || (s.wind ?? 1) < 0.8 ? "warn" : "", mi: cfg.ghi_entity, title: t(hass, "health_station_tip") })
        : chip(t(hass, "health_station"), t(hass, "health_station_none"), { tone: "off", mi: cfg.ghi_entity, title: t(hass, "health_station_tip") }));
    }

    // ---- tables, each on its own evidence -----------------------------------
    const covLast = a.coverage_last ?? {};
    const covLow = Object.values(covLast).some((v) => v < 0.95);
    // night stays in the detail view, dimmed — elsewhere it is not a finding
    const skips = Object.entries(lc?.skipped_because ?? {})
      .filter(([k]) => detail || k !== "night").sort((x, y) => y[1] - x[1]);
    const censDeviates = lower > 0 || recon > 0 || curtailed || covMeans.some((v) => v < 0.95);
    const sect = (label) => `<div class="hc-sect">${label}</div>`;
    let tables = "";
    if (Object.keys(covLast).length && (detail || covLow)) {
      tables += sect(t(hass, "health_per_string")) + `<table>${Object.entries(covLast).map(([id, v]) =>
        `<tr><th class="dim">${esc(nameOf(id))}</th><td class="${v < 0.8 ? "warm" : ""}"><span class="pvs-num">${fmtNum(hass, v * 100, 0)} %</span></td></tr>`).join("")}</table>`;
    }
    if (lc && (detail || skips.length)) {
      const counters = detail
        ? ["hours_materialised", "observations_used", "observations_skipped", "censored_hours",
           "bias_observations", "shading_observations", "ghi_hours_rejected", "reconstructed_intervals"]
          .filter((k) => lc[k] !== undefined)
          .map((k) => `<tr><th>${keyLabel(hass, k)}</th><td><span class="pvs-num">${lc[k]}</span></td></tr>`).join("")
        : "";
      tables += sect(t(hass, "nerd_skips")) + `<table>${counters}
        ${skips.length ? `<tr><th colspan="2" class="kv-sect">${keyLabel(hass, "skipped_because")}</th></tr>` : ""}
        ${skips.map(([k, v]) => `<tr><th class="dim">${esc(k)}</th><td class="${k === "night" ? "" : "warm"}"><span class="pvs-num">${v}</span></td></tr>`).join("")}
      </table>`;
    }
    if (sd && (detail || censDeviates)) {
      tables += sect(t(hass, "nerd_censoring")) + `<table><tr><th></th><th>${t(hass, "vk_measured")}</th><th>${t(hass, "vk_lower_bound")}</th><th>${t(hass, "vk_reconstructed")}</th><th>${t(hass, "cens_coverage")}</th><th>${t(hass, "cens_curtailed")}</th></tr>
        ${Object.entries(sd).map(([name, s]) => {
          const vk = s.today?.value_kinds ?? {};
          return `<tr><th>${esc(name)}</th>
            <td><span class="pvs-num">${vk.measured ?? 0}</span></td>
            <td class="${(vk.lower_bound ?? 0) > 0 ? "warm" : ""}"><span class="pvs-num">${vk.lower_bound ?? 0}</span></td>
            <td class="${(vk.reconstructed ?? 0) > 0 ? "warm" : ""}"><span class="pvs-num">${vk.reconstructed ?? 0}</span></td>
            <td><span class="pvs-num">${s.today?.coverage_mean != null ? fmtNum(hass, s.today.coverage_mean * 100, 0) + "%" : "—"}</span></td>
            <td class="${(s.today?.curtailed_fraction ?? 0) > 0 ? "warm" : ""}"><span class="pvs-num">${s.today?.curtailed_fraction != null ? fmtNum(hass, s.today.curtailed_fraction * 100, 0) + "%" : "—"}</span></td></tr>`;
        }).join("")}</table>`;
    }
    if (detail) {
      tables += sect(t(hass, "health_counters")) + `<table>${["intervals_written", "events_seen", "write_errors", "watchdog_ticks", "last_flush_duration_ms"]
        .filter((k) => a[k] !== undefined)
        .map((k) => `<tr><th>${keyLabel(hass, k)}</th><td><span class="pvs-num">${a[k]}</span></td></tr>`).join("")}
        ${a.last_error ? `<tr><th class="dim">last error</th><td class="hot">${esc(a.last_error)}</td></tr>` : ""}</table>`;
    }

    // one sentence when nothing deviates — the number, not a verdict
    const allGood = werr === 0 && a.weather_ok !== false && !covLow
      && (!lc || (skipped === 0 && (lc.censored_hours ?? 0) === 0))
      && !censDeviates;
    const nStr = Object.keys(sd ?? covLast).length;
    const good = allGood && nStr && !detail
      ? `<div class="hc-good">✓ ${tn(hass, "health_all_good", nStr, {
          iv: intervals.length ? fmtNum(hass, intervals.reduce((s, x) => s + x, 0) / intervals.length, 0) : "–",
          cov: covMeans.length ? fmtNum(hass, covMeans.reduce((s, x) => s + x, 0) / covMeans.length * 100, 0) : fmtNum(hass, cov ?? 0, 0) })}</div>`
      : "";

    card(`<div class="pvs-head">
        <span class="pvs-title clickable" data-more-info="${cfg.entity}">${esc(cfg.title ?? t(hass, "health_title"))}</span>
        ${helpChip("health")}
      </div>
      <div class="hc-row">${chips.join("")}</div>
      ${good}${tables ? `<div class="kv-scroll">${tables}</div>` : ""}`);
  }

  static getConfigElement() { return document.createElement("pvstrings-health-editor"); }
  static getStubConfig(hass, entities) {
    const guess = (entities ?? []).find((e) => hass.states[e]?.attributes?.intervals_written !== undefined);
    return { entity: guess ?? "" };
  }
}

const HEALTH_CSS = `
  .hc-row { display: flex; flex-wrap: wrap; gap: 8px; }
  .pvs-chip.hc { padding: 7px 11px 7px 9px; gap: 8px; border-color: var(--pvs-hairline); }
  .pvs-chip.hc::before { content: ""; width: 7px; height: 7px; border-radius: 50%; flex: none;
    background: var(--success-color, #43a047); }
  .pvs-chip.hc .k { color: var(--secondary-text-color); }
  .pvs-chip.hc .v { font-size: 12.5px; }
  .pvs-chip.hc.warn::before { background: var(--warning-color, #ffa600); }
  .pvs-chip.hc.hot { background: color-mix(in srgb, var(--error-color, #db4437) 12%, transparent); color: var(--primary-text-color); }
  .pvs-chip.hc.hot::before { background: var(--error-color, #db4437); }
  .pvs-chip.hc.off::before { background: var(--secondary-text-color); opacity: 0.45; }
  .pvs-chip.hc.off .v { color: var(--secondary-text-color); font-family: inherit; font-style: italic; }
  .hc-good { margin-top: 12px; font-size: 12px; color: var(--secondary-text-color); line-height: 1.5; }
  .hc-sect { margin-top: 14px; margin-bottom: 4px; font-size: 10px; letter-spacing: 0.6px; text-transform: uppercase; color: var(--secondary-text-color); }
`;

/* ========================= SECTION: CARD:THERMAL ========================= */

// Cell temperature over the day, one line per string, the air as a thin
// dashed line: the distance between the two is irradiance and wind. Below,
// what the heat costs — the running hour's factor and today's sum from the
// cell-temperature sensor (>= 1.24). Modelled, never measured, and the card
// says so. The share is already inside the chain's physics figure; this
// card only makes it visible.
class PvsThermalCard extends PvsBaseCard {
  getCardSize() { return 4; }
  getGridOptions() { return { columns: "full", rows: "auto" }; }
  watchedEntities() {
    return (this._config?.rows ?? []).flatMap((r) => [r.forecast, r.cell]).filter(Boolean);
  }

  _render() {
    const hass = this._hass, cfg = this._config;
    if (!hass || !cfg) return;
    const card = (inner) => {
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${FC_CSS}${KV_CSS}${TH_CSS}</style><ha-card>${inner}<div class="pvs-tip"></div></ha-card>`;
      this._wire();
    };
    const rows = (cfg.rows ?? []).filter((r) => r?.forecast);
    if (!rows.length) return card(problemHTML(hass, { reason: t(hass, "no_entity_config") }));
    const known = rows.filter((r) => hass.states[r.forecast]);
    if (!known.length) return card(problemHTML(hass, { reason: t(hass, "entity_missing", { entity: rows[0].forecast }) }));
    const withThermal = known.filter((r) => FEATURES.thermal.test(hass.states[r.forecast].attributes));
    if (!withThermal.length) {
      return card(problemHTML(hass, { entity: known[0].forecast,
        missing: requireFeatures(hass.states[known[0].forecast], ["thermal"]).missing }));
    }

    const nowMs = Date.now();
    const todayKey = localParts(hass, nowMs).dayKey;
    const series = withThermal.map((r, i) => {
      const pts = (hass.states[r.forecast].attributes.forecast ?? [])
        .map((row) => ({ ...row, ms: new Date(row.datetime).getTime() }))
        .filter((row) => localParts(hass, row.ms).dayKey === todayKey)
        .sort((x, y) => x.ms - y.ms)
        .map((row) => { const lp = localParts(hass, row.ms); return { ...row, h: lp.hour + lp.minute / 60 }; });
      return { name: r.name ?? r.forecast, id: r.forecast, cell: r.cell,
        color: `var(--pvs-cat-${i % 6})`, pts, lit: pts.filter((p) => p.cell_temp_c != null) };
    });
    const title = cfg.title ?? t(hass, "th_title");

    // plant share today, energy-weighted over the strings: Σ physics /
    // Σ (physics / thermal) — the handover's formula, never a mean of factors
    let sumP = 0, sumRef = 0;
    for (const s of series) for (const p of s.lit) {
      if ((p.physics_kwh ?? 0) > 0 && (p.thermal ?? 0) > 0) { sumP += p.physics_kwh; sumRef += p.physics_kwh / p.thermal; }
    }
    const plantPct = sumRef > 0 ? (sumP / sumRef - 1) * 100 : null;
    const head = `<div class="pvs-head">
      <span class="pvs-title clickable" data-more-info="${series[0].id}">${esc(title)}</span>
      ${plantPct != null ? `<span class="pvs-chip ${plantPct < -3 ? "warn" : ""}" title="${esc(t(hass, "th_note"))}">${t(hass, "th_plant_today")}
        <span class="v">${fmtSigned(hass, plantPct, 1)} %</span> ${t(hass, "th_by_heat")}</span>` : ""}
      ${helpChip("thermal")}
    </div>`;

    // ---- the per-string table (running hour, today's sum) --------------------
    const tableRows = series.filter((s) => s.cell).map((s) => {
      const st = hass.states[s.cell];
      const ca = st?.attributes ?? {};
      const temp = st && Number.isFinite(parseFloat(st.state)) ? parseFloat(st.state) : null;
      const eff = ca.thermal_factor != null ? (ca.thermal_factor - 1) * 100 : null;
      const tone = eff == null ? "" : eff < -8 ? "hot" : eff < -3 ? "warm" : "";
      const loss = ca.heat_loss_today_kwh;
      return `<tr><th class="clickable" data-more-info="${s.cell}"><span class="sw" style="background:${s.color}"></span>${esc(s.name)}</th>
        <td>${temp == null ? withheldHTML(t(hass, "not_available")) : `<span class="pvs-num">${fmtNum(hass, temp, 1)}</span> <span class="n">°C</span>`}</td>
        <td class="${tone}">${eff == null ? "—" : `<span class="pvs-num">${fmtSigned(hass, eff, 1)} %</span>`}</td>
        <td>${loss == null ? "—" : `<span class="pvs-num">${fmtSigned(hass, -loss, 3)}</span> <span class="n">kWh</span>`}</td></tr>`;
    }).join("");
    const table = tableRows ? `<table class="th-table"><tr><th></th><th>${t(hass, "th_cells")}</th><th>${t(hass, "th_effect")}</th><th>${t(hass, "th_today")}</th></tr>${tableRows}</table>` : "";

    const lit = series.flatMap((s) => s.lit);
    if (!lit.length) {
      return card(head + withheldHTML(t(hass, "th_no_rows")) + table
        + `<div class="kv-note dim">${t(hass, "th_note")}</div>`);
    }

    // ---- chart ---------------------------------------------------------------
    // Hourly means sit at the hour centre; straight segments between them —
    // a spline would draw a resolution the forecast does not have.
    const airPts = (series.find((s) => s.pts.length)?.pts ?? []).filter((p) => p.air_temp_c != null);
    const hStart = Math.floor(Math.min(...lit.map((p) => p.h)));
    const hEnd = Math.min(24, Math.ceil(Math.max(...lit.map((p) => p.h))) + 1);
    const inWin = (p) => p.h >= hStart && p.h < hEnd;
    const temps = [...lit.map((p) => p.cell_temp_c), ...airPts.filter(inWin).map((p) => p.air_temp_c)];
    let tMin = Math.floor((Math.min(...temps) - 2) / 5) * 5;
    let tMax = Math.ceil((Math.max(...temps) + 2) / 5) * 5;
    if (tMax - tMin < 10) tMax = tMin + 10;
    const PAD_L = 34, PAD_R = 10, PAD_T = 12, PAD_B = 22, PW = 560, PH = 130;
    const W = PAD_L + PW + PAD_R, H = PAD_T + PH + PAD_B;
    const xOf = (h) => PAD_L + ((h + 0.5 - hStart) / (hEnd - hStart)) * PW;
    const yOf = (v) => PAD_T + PH - ((v - tMin) / (tMax - tMin)) * PH;
    const step = tMax - tMin > 30 ? 10 : 5;
    let grid = "";
    for (let v = tMin; v <= tMax; v += step) {
      grid += `<line class="grid" x1="${PAD_L}" y1="${yOf(v).toFixed(1)}" x2="${W - PAD_R}" y2="${yOf(v).toFixed(1)}"/>
        <text class="axis" x="${PAD_L - 5}" y="${(yOf(v) + 3).toFixed(1)}" text-anchor="end">${fmtNum(hass, v, 0)}</text>`;
    }
    grid += `<text class="axis" x="${PAD_L - 5}" y="${PAD_T - 3}" text-anchor="end" style="font-size:8.5px">°C</text>`;
    const hourStep = hEnd - hStart > 12 ? 3 : 2;
    for (let h = hStart; h < hEnd; h += hourStep) {
      grid += `<text class="axis" x="${(xOf(h)).toFixed(1)}" y="${H - 6}" text-anchor="middle">${String(h).padStart(2, "0")}</text>`;
    }
    // the 25 °C reference the factor is measured against
    let ref = "";
    if (25 > tMin && 25 < tMax) {
      ref = `<line x1="${PAD_L}" y1="${yOf(25).toFixed(1)}" x2="${W - PAD_R}" y2="${yOf(25).toFixed(1)}"
          stroke="var(--secondary-text-color)" stroke-width="1" stroke-dasharray="2 3" opacity="0.7"/>
        <text class="axis" x="${W - PAD_R}" y="${(yOf(25) - 3).toFixed(1)}" text-anchor="end" style="font-size:8.5px">${t(hass, "th_ref")}</text>`;
    }
    const path = (pts, key) => pts.length < 2 ? "" : pts.map((p, i) =>
      `${i ? "L" : "M"}${xOf(p.h).toFixed(1)} ${yOf(p[key]).toFixed(1)}`).join("");
    const lines = series.map((s) => s.lit.length < 2
      ? s.lit.map((p) => `<circle cx="${xOf(p.h).toFixed(1)}" cy="${yOf(p.cell_temp_c).toFixed(1)}" r="2.5" fill="${s.color}"/>`).join("")
      : `<path d="${path(s.lit, "cell_temp_c")}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`).join("");
    const airLine = airPts.filter(inWin).length >= 2
      ? `<path d="${path(airPts.filter(inWin), "air_temp_c")}" fill="none" stroke="var(--secondary-text-color)" stroke-width="1.4" stroke-dasharray="4 3" opacity="0.85"/>` : "";
    // now marker, only while the window contains it
    const lpNow = localParts(hass, nowMs);
    const hNow = lpNow.hour + lpNow.minute / 60;
    const nowMark = hNow >= hStart && hNow < hEnd
      ? `<line x1="${(xOf(hNow - 0.5)).toFixed(1)}" y1="${PAD_T}" x2="${(xOf(hNow - 0.5)).toFixed(1)}" y2="${PAD_T + PH}" stroke="var(--pvs-sun)" stroke-width="1" opacity="0.8"/>
         <text class="axis" x="${(xOf(hNow - 0.5) + 4).toFixed(1)}" y="${PAD_T + 9}" style="fill:var(--pvs-sun)">${t(hass, "th_now")}</text>` : "";
    // hit slots per hour, tooltip lists every string
    let hits = "";
    const slotW = PW / (hEnd - hStart);
    for (let h = hStart; h < hEnd; h++) {
      const cells = series.map((s) => { const p = s.lit.find((q) => Math.floor(q.h) === h); return p ? { n: s.name, c: p.cell_temp_c, f: p.thermal ?? null } : null; }).filter(Boolean);
      if (!cells.length) continue;
      const air = airPts.find((p) => Math.floor(p.h) === h);
      const tip = { h, air: air?.air_temp_c ?? null, wind: air?.wind_ms ?? null, cells };
      hits += `<rect class="hit" x="${(PAD_L + (h - hStart) * slotW).toFixed(1)}" y="${PAD_T}" width="${slotW.toFixed(1)}" height="${PH}" fill="transparent" data-th='${esc(JSON.stringify(tip))}'/>`;
    }
    const legend = `<div class="pvs-legend">
      ${series.map((s) => `<span class="it"><span class="ln" style="border-color:${s.color}"></span>${esc(s.name)}</span>`).join("")}
      <span class="it"><span class="ln air"></span>${t(hass, "th_air")}</span>
    </div>`;

    card(`${head}
      <div class="fc-wrap"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="aspect-ratio:${W}/${H}">
        ${grid}${ref}${airLine}${lines}${nowMark}${hits}
      </svg></div>
      ${legend}${table}
      <div class="kv-note dim">${t(hass, "th_note")}</div>`);
  }

  _wire() {
    this._wireMoreInfo();
    if (this._wired) return;
    this._wired = true;
    wireTooltip(this, {
      selector: "[data-th]",
      content: (el) => {
        const hass = this._hass;
        const c = JSON.parse(el.getAttribute("data-th"));
        return `<div class="h">${String(c.h).padStart(2, "0")}:00–${String((c.h + 1) % 24).padStart(2, "0")}:00</div>
          ${c.cells.map((x) => `<div class="r"><span class="k">${esc(x.n)}</span><span class="v">${fmtNum(hass, x.c, 1)} °C${x.f != null ? ` · ${fmtSigned(hass, (x.f - 1) * 100, 1)} %` : ""}</span></div>`).join("")}
          ${c.air != null ? `<div class="r"><span class="k">${t(hass, "th_air")}</span><span class="v">${fmtNum(hass, c.air, 1)} °C</span></div>` : ""}
          ${c.wind != null ? `<div class="r"><span class="k">${t(hass, "th_wind")}</span><span class="v">${fmtNum(hass, c.wind, 1)} m/s</span></div>` : ""}`;
      },
    });
  }

  static getConfigElement() { return document.createElement("pvstrings-chain-editor"); }
  static getStubConfig() { return { rows: [] }; }
}

const TH_CSS = `
  .hit { cursor: crosshair; }
  .pvs-legend .ln { display: inline-block; width: 16px; height: 0; border-top: 2px solid var(--secondary-text-color); }
  .pvs-legend .ln.air { border-top-style: dashed; border-top-width: 1.5px; }
  .th-table { margin-top: 10px; }
  .th-table th .sw { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 7px; vertical-align: middle; }
  .th-table th.clickable { cursor: pointer; }
  .kv-note { font-size: 11px; padding: 5px 8px; border-radius: 6px; background: var(--pvs-chip-bg); color: var(--secondary-text-color); margin-top: 10px; line-height: 1.5; }
`;

/* ========================== SECTION: STRATEGY ============================ */

function mdCard(content) { return { type: "markdown", content }; }
function heading(text, style = "title") { return { type: "heading", heading: text, heading_style: style }; }

function tileOrMissing(hass, lang, node, key, extra = {}) {
  const id = node?.byKey?.[key];
  if (!id) return mdCard(t(lang, "missing_card", { key }));
  // Strip the device-name prefix: the section heading already names the
  // device, and full friendly names truncate in tiles.
  let name = extra.name;
  if (!name) {
    const friendly = hass.states[id]?.attributes?.friendly_name ?? "";
    if (node.name && friendly.startsWith(node.name)) {
      name = friendly.slice(node.name.length).trim();
    }
  }
  return { type: "tile", entity: id, ...(name ? { name } : {}), ...extra };
}

// For OPTIONAL entities (conversion layer): not configured means the entity
// does not exist, and nothing should appear — that is not a missing_card
// case, which is reserved for entities the contract promises.
function tileIf(hass, lang, node, key, extra = {}) {
  return node?.byKey?.[key] ? tileOrMissing(hass, lang, node, key, extra) : null;
}

async function buildViews(hass, config) {
  const lang = langOf(hass);
  const model = await getRegistryModel(hass);
  if (!model.plants.length) {
    return [{
      title: "PV Strings", path: "pv-strings",
      sections: [{ type: "grid", cards: [mdCard(t(lang, "strategy_no_integration"))] }],
      type: "sections",
    }];
  }
  const views = [];
  const multi = model.plants.length > 1;
  for (const plant of model.plants) {
    const prefix = multi ? `${plant.name} — ` : "";
    const slug = multi ? plant.name.toLowerCase().replace(/[^a-z0-9]+/g, "-") + "-" : "";
    const strings = model.strings.filter((s) => s.plant === plant);
    const groups = model.groups.filter((g) => g.plant === plant);
    const P = (key, extra) => tileOrMissing(hass, lang, plant, key, extra);
    const forecastDays = config?.forecast_days ?? 2;

    // ---- Übersicht ----
    const overviewSections = [
      { type: "grid", column_span: 2, cards: [
        heading(t(lang, "s_today")),
        P("produced_today", { color: "orange" }),
        P("forecast_today"),
        P("forecast_remaining"),
        P("power_now", { color: "orange" }),
        P("potential_now"),
        P("peak_hour_today"),
      ] },
      { type: "grid", column_span: 2, cards: [
        plant.byKey.forecast_today
          ? { type: "custom:pvstrings-forecast", entity: plant.byKey.forecast_today,
              days: forecastDays, show_unshaded: false }
          : mdCard(t(lang, "missing_card", { key: "forecast_today" })),
      ] },
      { type: "grid", cards: [
        heading(t(lang, "s_tomorrow")),
        P("forecast_tomorrow"),
        P("forecast_day_after"),
        P("rain_probability_tomorrow"),
      ] },
    ];
    // ---- conversion layer (>= 1.20, optional): own section right after the
    // forecast chart, deliberately NOT inside the DC groups section — AC and
    // battery charge must never read as summable with the DC tiles. Only the
    // figures anyone needs live here — what arrives behind the inverter; the
    // per-group charts (ratio per hour, curve, clipping) are on the Nerd view.
    const directGroups = groups.filter((g) => g.byKey.group_forecast_ac);
    const storageGroups = groups.filter((g) => g.byKey.group_forecast_battery_charge);
    if (directGroups.length || storageGroups.length || plant.byKey.forecast_ac_today) {
      const convCards = [heading(t(lang, "s_conversion"))];
      convCards.push(...[
        tileIf(hass, lang, plant, "forecast_ac_today"),
        tileIf(hass, lang, plant, "forecast_ac_tomorrow"),
      ].filter(Boolean));
      if (convCards.length > 1) overviewSections.splice(2, 0, { type: "grid", column_span: 2, cards: convCards });
    }
    if (groups.length) {
      overviewSections.push({ type: "grid", cards: [
        heading(t(lang, "s_groups")),
        ...groups.map((g) => tileOrMissing(hass, lang, g, "group_forecast_remaining", { name: g.name })),
      ] });
    }
    overviewSections.push({ type: "grid", column_span: 2, cards: [
      heading(t(lang, "s_savings")),
      P("savings_today"), P("savings_month"), P("savings_total"), P("amortisation"),
    ] });
    views.push({
      title: prefix + t(lang, "v_overview"), path: `${slug}overview`,
      icon: "mdi:white-balance-sunny", type: "sections", max_columns: 2,
      sections: overviewSections,
    });

    // ---- Stränge ----
    // The whole-plant Ist/Prognose chart leads the view at double width —
    // no shading series here (a plant has no single sky map).
    const plantLineSection = {
      type: "grid", column_span: 2, cards: [
        heading(plant.name),
        plant.byKey.forecast_today
          ? { type: "custom:pvstrings-forecast", entity: plant.byKey.forecast_today,
              days: 1, style: "line", wide: true, show_unshaded: false, title: plant.name }
          : mdCard(t(lang, "missing_card", { key: "forecast_today" })),
      ],
    };
    views.push({
      title: prefix + t(lang, "v_strings"), path: `${slug}strings`,
      icon: "mdi:solar-panel", type: "sections", max_columns: 2,
      sections: [plantLineSection, ...strings.map((s) => ({
        type: "grid", cards: [
          heading(s.name),
          s.byKey.string_forecast_today
            ? { type: "custom:pvstrings-forecast", entity: s.byKey.string_forecast_today, days: 1, style: "line", title: s.name }
            : mdCard(t(lang, "missing_card", { key: "string_forecast_today" })),
          s.byKey.string_sky_map
            ? { type: "custom:pvstrings-sky-map", entity: s.byKey.string_sky_map }
            : mdCard(t(lang, "missing_card", { key: "string_sky_map" })),
          tileOrMissing(hass, lang, s, "string_shading_now"),
          tileOrMissing(hass, lang, s, "string_produced_today", { color: "orange" }),
          tileOrMissing(hass, lang, s, "string_potential_now"),
          // modelled cell temperature (>= 1.24): optional, unknown at night
          ...[tileIf(hass, lang, s, "string_cell_temperature")].filter(Boolean),
        ],
      }))],
    });

    // ---- Genauigkeit ----
    // What anyone can read: forecast against actual, day by day, and whether
    // the learning pays. The figures (WMAPE, bias, both windows) are on the
    // Nerd view in full mode; `diagnostics: full` lays the six charts out side by side.
    const fullAcc = config?.diagnostics === "full";
    const accKeys = [...ACC_KEYS.short, ...ACC_KEYS.dayahead];
    const accEntities = Object.fromEntries(accKeys.filter((k) => plant.byKey[k]).map((k) => [k, plant.byKey[k]]));
    const dailySeries = [
      ...(plant.byKey.forecast_today ? [{ name: plant.name, entity: plant.byKey.forecast_today, days: 14 }] : []),
      ...strings.filter((s) => s.byKey.string_forecast_today)
        .map((s) => ({ name: s.name, entity: s.byKey.string_forecast_today, days: 14 })),
    ];
    const accSections = [
      fullAcc
        ? { type: "grid", column_span: 2, cards: [
            heading(t(lang, "s_daily"), "subtitle"),
            plant.byKey.forecast_today
              ? { type: "custom:pvstrings-daily", entity: plant.byKey.forecast_today, days: 14 }
              : mdCard(t(lang, "missing_card", { key: "forecast_today" })),
            ...strings.map((s) => s.byKey.string_forecast_today
              ? { type: "custom:pvstrings-daily", entity: s.byKey.string_forecast_today, days: 7, title: s.name }
              : mdCard(t(lang, "missing_card", { key: "string_forecast_today" }))),
          ] }
        : { type: "grid", column_span: 2, cards: [
            dailySeries.length
              ? { type: "custom:pvstrings-daily", series: dailySeries, grid_options: { columns: "full" } }
              : mdCard(t(lang, "missing_card", { key: "forecast_today" })),
          ] },
    ];
    // Learning progress (response service get_weeks): only where the
    // integration keeps weeks — an older one simply has no such section.
    if (hasPvsService(hass, "get_weeks") && plant.byKey.wmape_day_ahead_30d) {
      accSections.unshift({ type: "grid", column_span: 2, cards: [
        { type: "custom:pvstrings-learning", entity: plant.byKey.wmape_day_ahead_30d,
          grid_options: { columns: "full" } },
      ] });
    }
    // Where in the day the day-ahead error sits (PV Strings >= 1.23). The
    // attribute rides on the 30 d day-ahead sensor and is published before
    // that sensor has a state, so the gate is the attribute, never the state.
    // The card titles itself — no heading, or the title would read twice.
    // Readable without the figures behind it: a discount on tomorrow's hours.
    const da30 = plant.byKey.wmape_day_ahead_30d;
    if (da30 && Array.isArray(hass.states[da30]?.attributes?.hourly_profile)) {
      accSections.push({ type: "grid", column_span: 2, cards: [
        { type: "custom:pvstrings-hour-profile", entity: da30,
          grid_options: { columns: "full" } },
      ] });
    }
    views.push({
      title: prefix + t(lang, "v_accuracy"), path: `${slug}accuracy`,
      icon: "mdi:target", type: "sections", max_columns: 2, sections: accSections,
    });

    // ---- Nerd Dashboard ----
    // Status first, numbers on demand. Chips and charts carry the reading;
    // a table opens where a figure deviates. Ten tables over four screens
    // used to say "healthy" in developer vocabulary — nobody read them.
    // `diagnostics: full` on the strategy config restores every raw table,
    // n on every cell and the explainer footer: the debugging view, on
    // request, never by default.
    const full = config?.diagnostics === "full";
    const det = full ? { detail: true } : {};
    const mo = plant.byKey.model_observations;
    const ghi = plant.byKey.ghi_forecast;
    const coll = plant.byKey.collector_health;
    const sd = plant.byKey.strings_detail;
    const nerdSections = [];
    // Built with the weather source, placed after the thermal card — see
    // there for why.
    let sensorSection = null;
    // status: how far the training is, and whether collection runs at all —
    // the two questions every other card on this view rests on
    nerdSections.push({ type: "grid", column_span: 3, cards: [
      heading(t(lang, "s_status")),
      { type: "custom:pvstrings-maturity", ...(mo ? { entity: mo } : {}),
        rows: strings.map((s) => ({ name: s.name, sky: s.byKey.string_sky_map }))
          .filter((r) => r.sky) },
      coll
        ? { type: "custom:pvstrings-health", entity: coll,
            ...(mo ? { model_entity: mo } : {}), ...(sd ? { detail_entity: sd } : {}),
            ...(ghi ? { ghi_entity: ghi } : {}), ...det }
        : mdCard(t(lang, "missing_card", { key: "collector_health" })),
    ] });
    // The accuracy figures (WMAPE, bias, both windows) are raw numbers the
    // charts already tell: full mode only.
    if (full) {
      nerdSections.push({ type: "grid", column_span: 2, cards: [
        Object.keys(accEntities).length
          ? { type: "custom:pvstrings-accuracy", entities: accEntities, grid_options: { columns: "full" } }
          : mdCard(t(lang, "missing_card", { key: "wmape_30d" })),
      ] });
    }
    if (mo) {
      nerdSections.push({ type: "grid", cards: [
        heading(t(lang, "nerd_learning")),
        // Hourly buckets make these tables fourteen columns wide: full width
        // there, so the day fits without scrolling. Daypart integrations keep
        // their half-width three-column tables.
        ...(() => {
          const hourly = hass.states[mo]?.attributes?.daypart_scheme === 2;
          const wide = hourly ? { grid_options: { columns: "full" } } : {};
          return [
            { type: "custom:pvstrings-kv-table", entity: mo, mode: "log_ratio_plant",
              title: t(lang, hourly ? "nerd_plant_hours" : "nerd_plant_buckets"), ...det, ...wide },
            { type: "custom:pvstrings-kv-table", entity: mo, mode: "log_ratio_string_all",
              title: t(lang, hourly ? "nerd_strings_hours" : "nerd_strings_table"), ...det, ...wide },
          ];
        })(),
      ] });
      // What the weather SOURCE gets wrong, on its own. The sections view is
      // a plain grid: sections line up in rows and a row is as tall as its
      // tallest member, so three cards stacked here left the two columns
      // beside it standing empty for two card-heights.
      nerdSections.push({ type: "grid", cards: [
        heading(t(lang, "nerd_weather_source")),
        { type: "custom:pvstrings-kv-table", entity: mo, mode: "ghi_bias",
          title: t(lang, "bias_title"), ...(ghi ? { truth_entity: ghi } : {}), ...det },
      ] });
      // What the plant's own SENSOR says, in its own section: the nowcast
      // (what it says about the next two hours) and the report card (whether
      // it can be believed at all). Splitting them off is not only height —
      // the bias map is about the provider, these two are about the hardware
      // on the roof. Pushed further down; see the push site.
      if (ghi) {
        const sensorCards = [{ type: "custom:pvstrings-nowcast", entity: ghi }];
        // The check exists only where the attribute does AND a sensor is
        // configured — on a plant without one the block is all nulls, and a
        // card of zeros would be a claim about hardware that is not there.
        const ga = hass.states[ghi]?.attributes;
        const hasSensor = !!(ga?.ghi_entity || ga?.illuminance_entity);
        if (FEATURES.sensor_check.test(ga) && hasSensor) {
          sensorCards.push({ type: "custom:pvstrings-sensor-check", entity: ghi });
        }
        // What is being done about what the check found. The card asks the
        // service itself and withdraws where there is no trial, so the only
        // question here is whether there could be one at all.
        if (hasSensor && hasPvsService(hass, "calibration_trial")) {
          sensorCards.push({ type: "custom:pvstrings-calibration", entity: ghi });
        }
        sensorSection = { type: "grid", cards: [
          heading(t(lang, "nerd_sensor")), ...sensorCards,
        ] };
      }
    } else {
      nerdSections.push({ type: "grid", cards: [mdCard(t(lang, "missing_card", { key: "model_observations" }))] });
    }
    // Sky maps as a table: the shading charts on the strings view show the
    // same thing drawn — full mode only.
    if (full) {
      nerdSections.push({ type: "grid", cards: [
        { type: "custom:pvstrings-kv-table",
          entity: strings[0]?.byKey?.string_sky_map ?? sd ?? mo,
          mode: "sky_overview", title: t(lang, "nerd_sky"),
          rows: strings.map((s) => ({
            name: s.name, sky: s.byKey.string_sky_map, shading: s.byKey.string_shading_now,
          })) },
      ] });
    }
    // Thermal (PV Strings >= 1.24): only where a string chain carries the
    // heat share — absence is an older integration, not an error, so the
    // section simply does not exist there.
    const thermalRows = strings
      .filter((s) => s.byKey.string_forecast_today
        && FEATURES.thermal.test(hass.states[s.byKey.string_forecast_today]?.attributes))
      .map((s) => ({ name: s.name, forecast: s.byKey.string_forecast_today,
        ...(s.byKey.string_cell_temperature ? { cell: s.byKey.string_cell_temperature } : {}) }));
    if (thermalRows.length) {
      nerdSections.push({ type: "grid", cards: [
        { type: "custom:pvstrings-thermal", rows: thermalRows, grid_options: { columns: "full" } },
      ] });
    }
    // The sensor section goes last of the single-column ones, because the
    // sections view lines sections up in rows and a row is as tall as its
    // tallest member. Learning, the source and the heat are all about one
    // card-height; the sensor pair is half again as tall. Last means it
    // falls into the row that also holds the conversion block, which is
    // taller than all of them — so the tall one stands next to the tallest
    // and the short ones line up with each other.
    if (sensorSection) nerdSections.push(sensorSection);
    // conversion layer (optional): configuration + realized ratio per group
    const convNerd = groups.filter((g) =>
      (g.byKey.group_forecast_ac || g.byKey.group_forecast_battery_charge) && g.byKey.group_forecast_remaining);
    // one chart per group — direct paths first, storage after
    const convCharts = [...directGroups, ...storageGroups].map((g) => {
      const outKey = g.byKey.group_forecast_ac ? "group_forecast_ac" : "group_forecast_battery_charge";
      return g.byKey.group_forecast_remaining
        ? { type: "custom:pvstrings-conversion", entity: g.byKey[outKey],
            dc_entity: g.byKey.group_forecast_remaining, title: g.name,
            grid_options: { columns: "full" } }
        : mdCard(t(lang, "missing_card", { key: "group_forecast_remaining" }));
    });
    if (convNerd.length || convCharts.length) {
      // six columns: needs a double-width section and a card that opts into
      // its full grid width — same lesson as the censoring table
      const convEntity = convNerd[0]?.byKey.group_forecast_ac
        ?? convNerd[0]?.byKey.group_forecast_battery_charge;
      nerdSections.push({ type: "grid", column_span: 2, cards: [
        heading(t(lang, "nerd_conversion")),
        ...convCharts,
        ...(convNerd.length ? [{ type: "custom:pvstrings-kv-table",
          entity: convEntity,
          mode: "conversion", title: t(lang, "nerd_conv_paths"),
          grid_options: { columns: "full" },
          rows: convNerd.map((g) => ({
            name: g.name,
            out: g.byKey.group_forecast_ac ?? g.byKey.group_forecast_battery_charge,
            dc: g.byKey.group_forecast_remaining,
          })) },
        // collector evidence (rows/usable per stage): only rendered when the
        // integration collects — the card withholds otherwise, never errors
        { type: "custom:pvstrings-kv-table",
          entity: convEntity,
          mode: "conversion_evidence", title: t(lang, "nerd_conv_evidence"),
          grid_options: { columns: "full" } }] : []),
        // learned curve vs prior, per group that has a curve at all: the
        // storage path runs on fixed factors and never learns, a neutral
        // path has no curve to draw
        ...convNerd
          .filter((g) => {
            const id = g.byKey.group_forecast_ac;
            const src = id ? hass.states[id]?.attributes?.curve_source : null;
            return src && src !== "neutral" && src !== "fixed_factors";
          })
          .map((g) => ({ type: "custom:pvstrings-curve",
            entity: g.byKey.group_forecast_ac,
            title: `${t(lang, "curve_title")} — ${g.name}`,
            grid_options: { columns: "full" } })),
      ] });
    }
    // Full mode: the raw tables the health strip folded away, as they were.
    // Six columns next to long string names scroll inside a single column,
    // so the section is double width and each card opts into it.
    if (full) {
      nerdSections.push({ type: "grid", column_span: 2, cards: [
        heading(t(lang, "nerd_collection")),
        coll ? { type: "custom:pvstrings-kv-table", entity: coll, mode: "collector",
            title: t(lang, "nerd_collection"), grid_options: { columns: "full" } }
          : mdCard(t(lang, "missing_card", { key: "collector_health" })),
        mo ? { type: "custom:pvstrings-kv-table", entity: mo, mode: "skip_reasons",
            title: t(lang, "nerd_skips"), grid_options: { columns: "full" } }
          : mdCard(t(lang, "missing_card", { key: "model_observations" })),
        sd ? { type: "custom:pvstrings-kv-table", entity: sd, mode: "censoring",
            title: t(lang, "nerd_censoring"), grid_options: { columns: "full" } }
          : mdCard(t(lang, "missing_card", { key: "strings_detail" })),
      ] });
    }
    // Savings provenance (PV Strings >= 1.22, optional): what the money
    // rests on — which share of the energy was valued at a recorded price
    // and which fell back to the fixed tariff, plus grid export the strings
    // did not make. Both blocks only exist where they have something to say,
    // so a plant on a fixed tariff with a sane meter never sees this section
    // at all. Deliberately here and not on the overview: that view is for
    // reading, not for auditing.
    const savRows = [
      ["savings_today", "price_win_today"], ["savings_total", "price_win_total"],
    ].filter(([key]) => plant.byKey[key])
      .map(([key, label]) => ({ name: t(lang, label), entity: plant.byKey[key] }));
    const hasProvenance = savRows.some((r) => {
      const pa = hass.states[r.entity]?.attributes;
      return pa?.price || pa?.export_dropped_kwh != null;
    });
    if (hasProvenance) {
      nerdSections.push({ type: "grid", column_span: 2, cards: [
        { type: "custom:pvstrings-kv-table",
          entity: plant.byKey.savings_total ?? plant.byKey.savings_today,
          mode: "price", title: t(lang, "nerd_price"),
          grid_options: { columns: "full" }, rows: savRows },
      ] });
    }
    // The explainer footer survives only in full mode: every card now
    // carries its own "?" with the paragraph that belongs to it, next to
    // the numbers it explains rather than three screens below them.
    if (full) {
      const explBullets = t(lang, "nerd_explain").split("\n- ").map((b, i) => (i ? "- " + b : b));
      const explCols = [explBullets.slice(0, 3), explBullets.slice(3, 6), explBullets.slice(6)]
        .filter((c) => c.length).map((c) => mdCard(c.join("\n")));
      nerdSections.push({ type: "grid", column_span: 3, cards: [
        heading(t(lang, "nerd_explain_title")), ...explCols,
      ] });
    }
    views.push({
      // The path stays `nerd`: it is the URL people bookmark.
      title: prefix + t(lang, "v_nerd"), path: `${slug}nerd`,
      icon: "mdi:flask-outline", type: "sections", max_columns: 3,
      sections: nerdSections,
    });
  }
  return views;
}

class PvsDashboardStrategy {
  static async generate(config, hass) {
    return { views: await buildViews(hass, config) };
  }
}
class PvsViewStrategy {
  static async generate(config, hass) {
    const views = await buildViews(hass, config);
    const want = config?.view ?? "overview";
    return views.find((v) => v.path?.endsWith(want)) ?? views[0];
  }
}

/* ========================== SECTION: REGISTER ============================ */

const EDITORS = {
  "pvstrings-sky-map-editor": [ENTITY_SCHEMA,
    { name: "show_sun", selector: { boolean: {} } },
    { name: "seasons", selector: { boolean: {} } }],
  "pvstrings-forecast-editor": [ENTITY_SCHEMA,
    { name: "style", selector: { select: { options: ["bars", "line"], mode: "dropdown" } } },
    { name: "wide", selector: { boolean: {} } },
    { name: "smooth", selector: { boolean: {} } },
    { name: "days", selector: { number: { min: 1, max: 3, mode: "box" } } },
    { name: "show_unshaded", selector: { boolean: {} } },
    { name: "show_actual", selector: { boolean: {} } }],
  "pvstrings-chain-editor": [ENTITY_SCHEMA],
  "pvstrings-conversion-editor": [ENTITY_SCHEMA,
    { name: "dc_entity", selector: { entity: { filter: { integration: "pvstrings" } } } },
    { name: "title", selector: { text: {} } }],
  "pvstrings-daily-editor": [ENTITY_SCHEMA,
    { name: "days", selector: { number: { min: 3, max: 60, mode: "box" } } }],
  "pvstrings-hour-profile-editor": [ENTITY_SCHEMA,
    { name: "title", selector: { text: {} } }],
  "pvstrings-health-editor": [ENTITY_SCHEMA,
    { name: "model_entity", selector: { entity: { filter: { integration: "pvstrings" } } } },
    { name: "detail_entity", selector: { entity: { filter: { integration: "pvstrings" } } } },
    { name: "ghi_entity", selector: { entity: { filter: { integration: "pvstrings" } } } },
    { name: "detail", selector: { boolean: {} } }],
};
for (const [tag, schema] of Object.entries(EDITORS)) {
  if (!customElements.get(tag)) customElements.define(tag, makeEditor(schema));
}

const CARDS = [
  ["pvstrings-sky-map", PvsSkyMapCard, "PV Strings Sky Map",
    "The learned sky as a grid over sun position, with fit level and unobserved cells made explicit."],
  ["pvstrings-forecast", PvsForecastCard, "PV Strings Forecast",
    "Hourly forecast vs unshaded vs actual — the whole shading diagnostic in one chart."],
  ["pvstrings-nowcast", PvsNowcastCard, "PV Strings Nowcast",
    "How the measured clearness of the last quarter hour is blended into the coming forecast."],
  ["pvstrings-sensor-check", PvsSensorCheckCard, "PV Strings Sensor Check",
    "The plant's irradiance sensor against an independent archive, per sun elevation. Diagnosis only."],
  ["pvstrings-calibration", PvsCalibrationCard, "PV Strings Calibration Trial",
    "The second model branch reading the sensor through a correction curve, against the published one."],
  ["pvstrings-curve", PvsCurveCard, "PV Strings Conversion Curve",
    "The learned efficiency curve against the datasheet prior it started from."],
  ["pvstrings-conversion", PvsConversionCard, "PV Strings Conversion",
    "DC potential vs converted output (AC or battery charge), with an honest per-hour ratio strip."],
  ["pvstrings-chain", PvsChainCard, "PV Strings Chain",
    "What each model layer did to the raw physics for one hour, beside the measurement."],
  ["pvstrings-daily", PvsDailyCard, "PV Strings Daily",
    "Day-ahead forecast vs actual production, day by day."],
  ["pvstrings-hour-profile", PvsHourProfileCard, "PV Strings Hourly Profile",
    "Where in the day the day-ahead error sits: announced vs arrived per hour, with the deviation a window plan can use."],
  ["pvstrings-kv-table", PvsKvTableCard, "PV Strings KV Table",
    "Diagnostic tables (correction factors as ±%, source-bias heatmap, sky overview, conversion, savings provenance)."],
  ["pvstrings-maturity", PvsMaturityCard, "PV Strings Maturity",
    "How far the training has come: weather-bucket evidence and sun-path coverage."],
  ["pvstrings-learning", PvsLearningCard, "PV Strings Learning Progress",
    "Forecast error the learning avoided against the same integration without it, week by week and in total."],
  ["pvstrings-accuracy", PvsAccuracyCard, "PV Strings Accuracy",
    "Short-term and day-ahead accuracy on two lines, each with how full its window is."],
  ["pvstrings-health", PvsHealthCard, "PV Strings Health",
    "Collection and learn cycle as one row of chips; tables open only where a figure deviates."],
  ["pvstrings-thermal", PvsThermalCard, "PV Strings Cell Temperature",
    "Modelled cell temperature per string over the day, the air beside it, and what the heat costs."],
];
window.customCards = window.customCards ?? [];
for (const [tag, cls, name, description] of CARDS) {
  if (!customElements.get(tag)) customElements.define(tag, cls);
  if (!window.customCards.some((c) => c.type === tag)) {
    window.customCards.push({
      type: tag, name, description,
      preview: !["pvstrings-kv-table", "pvstrings-maturity", "pvstrings-health", "pvstrings-thermal", "pvstrings-accuracy", "pvstrings-learning", "pvstrings-sensor-check", "pvstrings-calibration"].includes(tag),
      documentationURL: "https://github.com/doccodyblue/ha-pvstrings-dash",
    });
  }
}

if (!customElements.get("ll-strategy-dashboard-pvstrings")) {
  customElements.define("ll-strategy-dashboard-pvstrings", PvsDashboardStrategy);
}
if (!customElements.get("ll-strategy-view-pvstrings")) {
  customElements.define("ll-strategy-view-pvstrings", PvsViewStrategy);
}
window.customStrategies = window.customStrategies ?? [];
if (!window.customStrategies.some((s) => s.type === "pvstrings")) {
  window.customStrategies.push({
    type: "pvstrings", strategyType: "dashboard",
    name: "PV Strings", description: "Zero-config dashboard for the PV Strings integration.",
  });
}

console.info(
  `%c PVSTRINGS-DASH %c v${PVS_VERSION} %c cards + strategy for PV Strings ≥ ${PVS_MIN_INTEGRATION}`,
  "background:#4c319f;color:#fff;padding:2px 6px;border-radius:3px 0 0 3px;font-weight:600",
  "background:#2a78d6;color:#fff;padding:2px 6px;border-radius:0 3px 3px 0",
  "color:#888"
);
