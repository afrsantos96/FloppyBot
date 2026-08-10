(function () {
  "use strict";

  const MANUAL_VALUE = "__manual__";
  const EMPTY_VALUE = "__empty__";

  const statusLine = document.getElementById("status-line");
  const tabsEl = document.getElementById("tabs");
  const sectionsEl = document.getElementById("sections");
  const saveBtn = document.getElementById("save-btn");
  const pendingCountEl = document.getElementById("pending-count");

  let snapshot = null;      // last server response
  let initialState = {};    // key -> {fid, manual_name}

  function slotKey(position, time) {
    return position + "|" + time;
  }

  function setStatus(text, kind) {
    statusLine.textContent = text;
    statusLine.className = "status-line" + (kind ? " msg-" + kind : "");
  }

  async function loadSchedule() {
    setStatus("Loading schedule…");
    const res = await fetch("/api/schedule");
    if (res.status === 401) {
      setStatus("Your session expired. Ask Discord for a new portal link.", "error");
      return;
    }
    if (res.status === 403) {
      setStatus("You do not have permission to manage minister appointments.", "error");
      return;
    }
    if (!res.ok) {
      setStatus("Failed to load schedule (HTTP " + res.status + ").", "error");
      return;
    }
    snapshot = await res.json();
    initialState = {};
    for (const position of snapshot.positions) {
      const slots = snapshot.appointments[position] || {};
      for (const time of Object.keys(slots)) {
        const entry = slots[time];
        initialState[slotKey(position, time)] = { fid: entry.fid || null, manual_name: entry.manual_name || null };
      }
    }
    render();
    setStatus("Loaded. Editing as " + (snapshot.is_global ? "global admin" : "alliance admin") + ".");
  }

  function render() {
    tabsEl.innerHTML = "";
    sectionsEl.innerHTML = "";

    snapshot.positions.forEach((position, idx) => {
      const tabBtn = document.createElement("button");
      tabBtn.className = "tab-btn" + (idx === 0 ? " active" : "");
      tabBtn.type = "button";
      tabBtn.textContent = position;
      tabBtn.dataset.position = position;
      tabBtn.addEventListener("click", () => selectTab(position));
      tabsEl.appendChild(tabBtn);

      const section = document.createElement("section");
      section.className = "section" + (idx === 0 ? " active" : "");
      section.dataset.position = position;
      section.appendChild(buildTable(position));
      sectionsEl.appendChild(section);
    });

    updatePendingCount();
  }

  function selectTab(position) {
    tabsEl.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.position === position);
    });
    sectionsEl.querySelectorAll(".section").forEach((sec) => {
      sec.classList.toggle("active", sec.dataset.position === position);
    });
  }

  function buildTable(position) {
    const table = document.createElement("table");
    const thead = document.createElement("thead");
    thead.innerHTML = "<tr><th>Time</th><th>Assignee</th></tr>";
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    const times = snapshot.time_slots[position] || [];
    const slots = snapshot.appointments[position] || {};

    times.forEach((time) => {
      const entry = slots[time] || {};
      const row = document.createElement("tr");
      row.dataset.position = position;
      row.dataset.time = time;

      const timeCell = document.createElement("td");
      timeCell.className = "time-cell";
      timeCell.textContent = time;
      row.appendChild(timeCell);

      const controlCell = document.createElement("td");
      const controls = document.createElement("div");
      controls.className = "row-controls";

      const select = document.createElement("select");
      select.appendChild(new Option("— empty —", EMPTY_VALUE));
      snapshot.members.forEach((m) => {
        const label = m.nickname + " (" + m.fid + ")" + (m.alliance_name ? " — " + m.alliance_name : "");
        select.appendChild(new Option(label, String(m.fid)));
      });
      select.appendChild(new Option("— manual name —", MANUAL_VALUE));

      const manualInput = document.createElement("input");
      manualInput.type = "text";
      manualInput.maxLength = 100;
      manualInput.placeholder = "Type a name…";
      manualInput.style.display = "none";

      if (entry.fid) {
        select.value = String(entry.fid);
      } else if (entry.manual_name) {
        select.value = MANUAL_VALUE;
        manualInput.value = entry.manual_name;
        manualInput.style.display = "";
      } else {
        select.value = EMPTY_VALUE;
      }

      select.addEventListener("change", () => {
        manualInput.style.display = select.value === MANUAL_VALUE ? "" : "none";
        markDirtyState(row);
      });
      manualInput.addEventListener("input", () => markDirtyState(row));

      controls.appendChild(select);
      controls.appendChild(manualInput);
      controlCell.appendChild(controls);
      row.appendChild(controlCell);

      tbody.appendChild(row);
    });

    table.appendChild(tbody);
    return table;
  }

  function readRowState(row) {
    const select = row.querySelector("select");
    const manualInput = row.querySelector("input[type='text']");
    if (select.value === EMPTY_VALUE) {
      return { fid: null, manual_name: null };
    }
    if (select.value === MANUAL_VALUE) {
      const name = manualInput.value.trim();
      return { fid: null, manual_name: name || null };
    }
    return { fid: parseInt(select.value, 10), manual_name: null };
  }

  function statesEqual(a, b) {
    return a.fid === b.fid && (a.manual_name || null) === (b.manual_name || null);
  }

  function markDirtyState(row) {
    const key = slotKey(row.dataset.position, row.dataset.time);
    const before = initialState[key] || { fid: null, manual_name: null };
    const after = readRowState(row);
    row.classList.toggle("dirty", !statesEqual(before, after));
    updatePendingCount();
  }

  function collectChanges() {
    const changes = [];
    document.querySelectorAll("tbody tr").forEach((row) => {
      const key = slotKey(row.dataset.position, row.dataset.time);
      const before = initialState[key] || { fid: null, manual_name: null };
      const after = readRowState(row);
      if (statesEqual(before, after)) return;

      if (!after.fid && !after.manual_name) {
        changes.push({ appointment_type: row.dataset.position, time: row.dataset.time, clear: true });
      } else if (after.fid) {
        changes.push({ appointment_type: row.dataset.position, time: row.dataset.time, fid: after.fid });
      } else {
        changes.push({ appointment_type: row.dataset.position, time: row.dataset.time, manual_name: after.manual_name });
      }
    });
    return changes;
  }

  function updatePendingCount() {
    const dirtyRows = document.querySelectorAll("tbody tr.dirty").length;
    pendingCountEl.textContent = dirtyRows > 0 ? dirtyRows + " unsaved change" + (dirtyRows === 1 ? "" : "s") : "";
    saveBtn.disabled = dirtyRows === 0;
  }

  async function saveChanges() {
    const changes = collectChanges();
    if (changes.length === 0) return;

    saveBtn.disabled = true;
    setStatus("Saving…");

    try {
      const res = await fetch("/api/schedule", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ changes }),
      });

      if (res.status === 401) {
        setStatus("Your session expired. Ask Discord for a new portal link.", "error");
        return;
      }

      const body = await res.json();
      if (!res.ok) {
        setStatus("Save failed: " + (body.error || res.status), "error");
        return;
      }

      let msg = "Saved " + body.applied + " change" + (body.applied === 1 ? "" : "s") + ".";
      if (body.conflicts && body.conflicts.length > 0) {
        msg += " " + body.conflicts.length + " row(s) were skipped (invalid or not permitted).";
      }
      setStatus(msg, body.conflicts && body.conflicts.length ? "error" : "success");

      await loadSchedule();
    } catch (err) {
      setStatus("Save failed: " + err, "error");
    } finally {
      updatePendingCount();
    }
  }

  saveBtn.addEventListener("click", saveChanges);

  loadSchedule();
})();
