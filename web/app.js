"use strict";

const $ = (id) => document.getElementById(id);
const sampleSelect = $("sample");
const payloadBox = $("payload");
const runButton = $("run");
const validateButton = $("validate");
const statusText = $("status");
const validationBox = $("validation");

let samples = [];

// ---------------------------------------------------------------- samples
async function loadSamples() {
  try {
    const response = await fetch("../samples");
    samples = await response.json();
    sampleSelect.innerHTML =
      '<option value="">-- choose a sample --</option>' +
      samples.map((s, i) => `<option value="${i}">${s.id} — ${s.label}</option>`).join("");
  } catch (err) {
    sampleSelect.innerHTML = '<option value="">(samples unavailable)</option>';
  }
}

sampleSelect.addEventListener("change", () => {
  const sample = samples[Number(sampleSelect.value)];
  if (sample) {
    payloadBox.value = JSON.stringify(sample.input, null, 2);
    clearValidation();
  }
});

// ---------------------------------------------------------------- file upload
$("file").addEventListener("change", (event) => {
  const file = event.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    payloadBox.value = reader.result;
    sampleSelect.value = "";
    statusText.textContent = `loaded ${file.name}`;
    validate(true);
  };
  reader.onerror = () => showValidation(["Could not read that file."], false);
  reader.readAsText(file);
  event.target.value = "";  // allow re-uploading the same file
});

$("clear").addEventListener("click", () => {
  payloadBox.value = "";
  sampleSelect.value = "";
  clearValidation();
  $("results").classList.add("hidden");
  $("error").classList.add("hidden");
  statusText.textContent = "";
});

// ---------------------------------------------------------------- validation
const DIRECTIVE_HOUR_FIELDS = ["hour", "demand_kwh", "solar_kwh", "tariff_bdt_per_kwh"];
const BATTERY_FIELDS = [
  "capacity_kwh", "initial_energy_kwh", "minimum_energy_kwh",
  "max_charge_kwh_per_hour", "max_discharge_kwh_per_hour",
];

const isNumber = (v) => typeof v === "number" && Number.isFinite(v);

/** Mirrors the Problem Statement request schema so bad input never leaves the page. */
function validateScenario(data) {
  const problems = [];

  if (data === null || typeof data !== "object" || Array.isArray(data)) {
    return ["Top level must be a JSON object."];
  }

  // scenario_id
  if (typeof data.scenario_id !== "string" || !data.scenario_id.trim()) {
    problems.push("scenario_id must be a non-empty string.");
  }

  // operator_notes
  if (!Array.isArray(data.operator_notes)) {
    problems.push("operator_notes must be an array of 1 to 3 strings.");
  } else {
    if (data.operator_notes.length < 1 || data.operator_notes.length > 3) {
      problems.push(`operator_notes must hold 1 to 3 notes (found ${data.operator_notes.length}).`);
    }
    data.operator_notes.forEach((note, i) => {
      if (typeof note !== "string" || !note.trim()) {
        problems.push(`operator_notes[${i}] must be a non-empty string.`);
      }
    });
  }

  // hours
  if (!Array.isArray(data.hours)) {
    problems.push("hours must be an array of exactly 24 entries.");
  } else {
    if (data.hours.length !== 24) {
      problems.push(`hours must hold exactly 24 entries (found ${data.hours.length}).`);
    }
    const seen = new Set();
    data.hours.forEach((entry, i) => {
      if (entry === null || typeof entry !== "object") {
        problems.push(`hours[${i}] must be an object.`);
        return;
      }
      DIRECTIVE_HOUR_FIELDS.forEach((field) => {
        if (!(field in entry)) problems.push(`hours[${i}] is missing ${field}.`);
        else if (!isNumber(entry[field])) problems.push(`hours[${i}].${field} must be a number.`);
      });
      if (isNumber(entry.hour)) {
        if (!Number.isInteger(entry.hour) || entry.hour < 0 || entry.hour > 23) {
          problems.push(`hours[${i}].hour must be an integer 0-23 (found ${entry.hour}).`);
        } else if (seen.has(entry.hour)) {
          problems.push(`hour ${entry.hour} appears more than once.`);
        } else {
          seen.add(entry.hour);
        }
      }
      if (isNumber(entry.demand_kwh) && entry.demand_kwh < 0) {
        problems.push(`hours[${i}].demand_kwh cannot be negative.`);
      }
      if (isNumber(entry.solar_kwh) && entry.solar_kwh < 0) {
        problems.push(`hours[${i}].solar_kwh cannot be negative.`);
      }
    });
    if (data.hours.length === 24 && seen.size === 24) {
      const missing = [];
      for (let h = 0; h < 24; h++) if (!seen.has(h)) missing.push(h);
      if (missing.length) problems.push(`missing hours: ${missing.join(", ")}.`);
    }
  }

  // battery
  if (data.battery === null || typeof data.battery !== "object" || Array.isArray(data.battery)) {
    problems.push("battery must be an object.");
  } else {
    BATTERY_FIELDS.forEach((field) => {
      if (!(field in data.battery)) problems.push(`battery is missing ${field}.`);
      else if (!isNumber(data.battery[field])) problems.push(`battery.${field} must be a number.`);
      else if (data.battery[field] < 0) problems.push(`battery.${field} cannot be negative.`);
    });
    const b = data.battery;
    if (isNumber(b.capacity_kwh) && b.capacity_kwh <= 0) {
      problems.push("battery.capacity_kwh must be greater than 0.");
    }
    if (isNumber(b.initial_energy_kwh) && isNumber(b.capacity_kwh)
        && b.initial_energy_kwh > b.capacity_kwh) {
      problems.push("battery.initial_energy_kwh cannot exceed capacity_kwh.");
    }
    if (isNumber(b.minimum_energy_kwh) && isNumber(b.capacity_kwh)
        && b.minimum_energy_kwh > b.capacity_kwh) {
      problems.push("battery.minimum_energy_kwh cannot exceed capacity_kwh.");
    }
    if (isNumber(b.initial_energy_kwh) && isNumber(b.minimum_energy_kwh)
        && b.initial_energy_kwh < b.minimum_energy_kwh) {
      problems.push("battery.initial_energy_kwh is below minimum_energy_kwh.");
    }
  }

  return problems;
}

/** Parse + validate what is in the textarea. Returns the object, or null. */
function validate(quiet) {
  const text = payloadBox.value.trim();
  if (!text) {
    showValidation(["Paste a scenario JSON or upload a file first."], false);
    return null;
  }

  let data;
  try {
    data = JSON.parse(text);
  } catch (err) {
    showValidation([`Not valid JSON: ${err.message}`], false);
    return null;
  }

  const problems = validateScenario(data);
  if (problems.length) {
    showValidation(problems, false);
    return null;
  }

  showValidation(
    [`Valid scenario "${data.scenario_id}" with ${data.operator_notes.length} `
     + `operator note(s) and 24 hourly entries.`], true);
  if (quiet) statusText.textContent = "";
  return data;
}

function showValidation(messages, ok) {
  validationBox.className = `validation ${ok ? "ok" : "bad"}`;
  validationBox.innerHTML = (ok ? "" : "<strong>Fix before sending:</strong>")
    + "<ul>" + messages.map((m) => `<li>${escapeHtml(m)}</li>`).join("") + "</ul>";
  validationBox.classList.remove("hidden");
}

function clearValidation() {
  validationBox.classList.add("hidden");
  validationBox.innerHTML = "";
}

validateButton.addEventListener("click", () => validate(false));
payloadBox.addEventListener("input", clearValidation);

// ---------------------------------------------------------------- run
runButton.addEventListener("click", async () => {
  const body = validate(true);
  if (!body) {
    statusText.textContent = "not sent - fix the JSON above";
    return;
  }

  runButton.disabled = true;
  statusText.textContent = "optimizing...";
  $("error").classList.add("hidden");
  const started = performance.now();

  try {
    const response = await fetch("../optimize-energy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) {
      return fail(`HTTP ${response.status}\n` + JSON.stringify(data, null, 2));
    }
    render(data, body);
    statusText.textContent = `done in ${((performance.now() - started) / 1000).toFixed(2)}s`;
  } catch (err) {
    fail(String(err));
  } finally {
    runButton.disabled = false;
  }
});

function fail(message) {
  const box = $("error");
  box.textContent = message;
  box.classList.remove("hidden");
  $("results").classList.add("hidden");
  statusText.textContent = "";
}

// ---------------------------------------------------------------- render
function render(data, request) {
  $("results").classList.remove("hidden");
  $("error").classList.add("hidden");

  renderNotes(data.directive_interpretation, request.operator_notes);

  $("t-grid").textContent = round(data.total_grid_kwh);
  $("t-cost").textContent = round(data.total_cost_bdt);
  $("t-peak").textContent = round(data.peak_grid_kwh);
  $("summary").textContent = data.plan_summary || "";
  $("raw").textContent = JSON.stringify(data, null, 2);

  renderPlan(data.hourly_plan);
}

// Each operator note, the directive it became, and the HOURS it affects.
function renderNotes(interpretations, notes) {
  $("notes").innerHTML = interpretations.map((entry) => {
    const noteText = (notes && notes[entry.note_index]) || "";
    const adjustment = entry.structured_adjustment || {};
    const hours = adjustment.hours || [];

    let valueBadge = "";
    if (adjustment.factor !== undefined) {
      valueBadge = `<span class="badge val">factor ${adjustment.factor}
        (${Math.round((1 - adjustment.factor) * 100)}% reduction)</span>`;
    } else if (adjustment.minimum_energy_kwh !== undefined) {
      valueBadge = `<span class="badge val">min ${adjustment.minimum_energy_kwh} kWh</span>`;
    } else if (adjustment.max_grid_kwh !== undefined) {
      valueBadge = `<span class="badge val">max ${adjustment.max_grid_kwh} kWh</span>`;
    }

    const strip = Array.from({ length: 24 }, (_, h) =>
      `<div class="${hours.includes(h) ? "on" : ""}">${h}</div>`).join("");

    const hoursBlock = hours.length
      ? `<div class="hours-label">Affected hours
           <span class="hours-list">[${hours.join(", ")}]</span></div>
         <div class="strip">${strip}</div>`
      : `<div class="hours-label">No hours affected</div>`;

    return `
      <div class="note ${entry.applies ? "applies" : ""}">
        <div class="note-head">
          <span class="badge">note ${entry.note_index}</span>
          <span class="badge type">${entry.directive_type}</span>
          <span class="badge ${entry.applies ? "yes" : "no"}">
            applies: ${entry.applies}</span>
          ${valueBadge}
        </div>
        <p class="note-text">${escapeHtml(noteText)}</p>
        ${hoursBlock}
        <p class="note-expl">${escapeHtml(entry.explanation || "")}</p>
      </div>`;
  }).join("");
}

function renderPlan(plan) {
  const peak = Math.max(...plan.map(
    (p) => p.grid_kwh + p.solar_used_kwh + p.battery_kwh), 1);

  document.querySelector("#plan tbody").innerHTML = plan.map((p) => {
    const width = (value) => `width:${(value / peak) * 100}%`;
    return `
      <tr>
        <td>${p.hour}</td>
        <td>${round(p.grid_kwh)}</td>
        <td>${round(p.solar_used_kwh)}</td>
        <td class="act"><span class="tag ${p.battery_action}">${p.battery_action}</span></td>
        <td>${round(p.battery_kwh)}</td>
        <td>${round(p.battery_energy_after_kwh)}</td>
        <td>
          <div class="bar">
            <i class="g" style="${width(p.grid_kwh)}"></i>
            <i class="s" style="${width(p.solar_used_kwh)}"></i>
            <i class="b" style="${width(p.battery_action === "discharge" ? p.battery_kwh : 0)}"></i>
          </div>
        </td>
      </tr>`;
  }).join("");
}

const round = (n) => (Math.round(Number(n) * 100) / 100).toLocaleString();

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

loadSamples();
