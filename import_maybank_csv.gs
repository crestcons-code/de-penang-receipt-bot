/**
 * Daily import of Maybank CSV statements into the DE Penang dana list.
 *
 * Replaces the manual copy-and-paste: reads the CSVs your bank exports into a
 * Drive folder and appends any transaction not already in the dana list, into
 * that month's tab, filling only columns A-F. Columns G onward (Receipts No,
 * accounting code, donor name, mobile...) are never touched - that is the
 * volunteers' working area.
 *
 * IMPORTANT - why duplicate checking matters here:
 * the Maybank export is MONTH-TO-DATE, not one day. A file downloaded on the
 * 29th contains every transaction since the 1st. Run daily, this job therefore
 * re-reads hundreds of transactions it has already imported, so it compares
 * against what is already in the sheet and appends only the genuinely new ones.
 * Running it twice in a row is harmless.
 *
 * SETUP (once):
 *   1. https://script.google.com -> New project, paste this file in, Save
 *   2. Fill in SHEET_ID below (the long code in your dana list's URL)
 *   3. Run  importMaybankCsv  once and approve the permission prompt.
 *      Check the log, and check the sheet looks right.
 *   4. Run  createDailyTrigger  once to schedule it every morning.
 */

// The long code in your dana list URL: .../spreadsheets/d/THIS_PART/edit
var SHEET_ID = 'PASTE_YOUR_SPREADSHEET_ID_HERE';

var CSV_FOLDER_NAME = 'DEP maybank csv';

// Only look at files touched recently. Older files hold nothing new, and this
// keeps each run fast. Duplicate checking still protects against overlap.
var LOOKBACK_DAYS = 10;

// Optional: email address to notify when rows are added or something fails.
// Leave '' for no email (the run still writes to the Apps Script log).
var NOTIFY_EMAIL = '';

/** Main entry point - run daily by the trigger. */
function importMaybankCsv() {
  if (SHEET_ID === 'PASTE_YOUR_SPREADSHEET_ID_HERE') {
    throw new Error('Set SHEET_ID first - see the comment above it.');
  }

  var ss = SpreadsheetApp.openById(SHEET_ID);
  var folders = DriveApp.getFoldersByName(CSV_FOLDER_NAME);
  if (!folders.hasNext()) {
    throw new Error('Drive folder not found: ' + CSV_FOLDER_NAME);
  }
  var folder = folders.next();

  var cutoff = new Date(new Date().getTime() - LOOKBACK_DAYS * 24 * 60 * 60 * 1000);
  var txns = [];
  var filesRead = 0;

  var it = folder.getFiles();
  while (it.hasNext()) {
    var file = it.next();
    if (file.getName().toLowerCase().indexOf('.csv') === -1) continue;
    if (file.getLastUpdated() < cutoff) continue;
    txns = txns.concat(parseMaybankCsv_(file.getBlob().getDataAsString()));
    filesRead++;
  }

  if (!txns.length) {
    log_('No transactions found in ' + filesRead + ' recent file(s). Nothing to do.');
    return;
  }

  // Group by the month each transaction belongs to, so one run can span a
  // month boundary (e.g. the file covering both 31 Jul and 01 Aug).
  var byMonth = {};
  txns.forEach(function (t) {
    (byMonth[t.yyyymm] = byMonth[t.yyyymm] || []).push(t);
  });

  var report = [];
  var totalAdded = 0;
  var skipped = 0;

  Object.keys(byMonth).sort().forEach(function (yyyymm) {
    var sheet = findMonthSheet_(ss, yyyymm);
    if (!sheet) {
      // A missing tab has to be shouted about, not just logged. At the start of
      // a new month this would otherwise drop every transaction silently, and
      // nobody would notice until the receipts stopped adding up.
      skipped += byMonth[yyyymm].length;
      report.push('WARNING: no tab named ' + yyyymm + ' - ' + byMonth[yyyymm].length +
                  ' row(s) NOT imported. Create that tab and the next run catches up.');
      return;
    }
    var added = appendNewRows_(sheet, byMonth[yyyymm]);
    totalAdded += added;
    report.push(yyyymm + ': ' + added + ' new row(s) added' +
                ' (' + byMonth[yyyymm].length + ' seen in CSV).');
  });

  var summary = 'Read ' + filesRead + ' file(s), ' + txns.length + ' cash-in transaction(s).\n' +
                report.join('\n');
  log_(summary);

  // Email on new rows OR on anything skipped - silence should mean "nothing
  // happened", never "something went wrong and you weren't told".
  if (NOTIFY_EMAIL && (totalAdded > 0 || skipped > 0)) {
    var subject = skipped > 0
      ? 'Dana list: ACTION NEEDED - ' + skipped + ' bank row(s) not imported'
      : 'Dana list: ' + totalAdded + ' new bank row(s)';
    MailApp.sendEmail(NOTIFY_EMAIL, subject, summary);
  }
}

/**
 * Parse one Maybank CSV export.
 * Layout: 3 info rows, then a header row, then data. Every field is wrapped in
 * quotes and prefixed with a tab character, so everything needs trimming.
 * Returns only cash-in rows (the dana list records incoming donations only).
 */
function parseMaybankCsv_(content) {
  var rows = Utilities.parseCsv(content);
  if (rows.length < 5) return [];

  // Locate the header row rather than assuming it is row 4 - Maybank has
  // changed the number of leading info rows before.
  var headerRow = -1;
  for (var i = 0; i < Math.min(rows.length, 10); i++) {
    if (clean_(rows[i][0]) === 'Transaction Date') { headerRow = i; break; }
  }
  if (headerRow === -1) return [];

  var header = rows[headerRow].map(clean_);
  var col = function (name) { return header.indexOf(name); };

  var iDate = col('Transaction Date');
  var iD1   = col('Transaction Description 1');
  var iD2   = col('Transaction Description 2');
  var iBen  = col('Beneficiary/ Biller Name');
  var iAcc  = col('MBB Receiving/Paying Account');
  var iIn   = col('Transaction Amount: Cash-in (RM)');

  if (iDate < 0 || iIn < 0) return [];

  var out = [];
  for (var r = headerRow + 1; r < rows.length; r++) {
    var row = rows[r];
    if (!row || row.length <= iIn) continue;

    var amount = toAmount_(row[iIn]);
    if (!(amount > 0)) continue;          // skip cash-out and blank lines

    var dateText = clean_(row[iDate]);    // e.g. "28 Jul 2026"
    var yyyymm = toYyyymm_(dateText);
    if (!yyyymm) continue;

    out.push({
      dateText: dateText,
      // Kept exactly as the bank wrote it, including the trailing "*" on
      // beneficiary names, so imported rows look identical to the ones
      // volunteers have been pasting in by hand.
      desc1: clean_(row[iD1]),
      desc2: clean_(row[iD2]),
      beneficiary: clean_(row[iBen]),
      account: clean_(row[iAcc]),
      amount: amount,
      yyyymm: yyyymm
    });
  }
  return out;
}

/**
 * Append transactions that are not already present.
 *
 * Matching uses ONLY date + amount - the two fields the bank controls and
 * nobody edits afterwards. It deliberately ignores the donor-name and
 * description columns: volunteers routinely rewrite those (replacing a bank
 * name with the real donor, adding a reference), and keying on them made
 * already-imported rows look new, which would re-import them as duplicates.
 *
 * Counts occurrences rather than just testing existence, so if two separate
 * donors each gave the same amount on the same day, both rows are kept - and
 * a day already fully imported adds nothing on the next run.
 */
function appendNewRows_(sheet, txns) {
  var lastRow = sheet.getLastRow();
  var existing = {};

  if (lastRow > 1) {
    // Columns A-F of every existing row
    var values = sheet.getRange(2, 1, lastRow - 1, 6).getValues();
    values.forEach(function (v) {
      var key = rowKey_(v[0], v[5]);
      if (key) existing[key] = (existing[key] || 0) + 1;
    });
  }

  var toAdd = [];
  txns.forEach(function (t) {
    var key = rowKey_(t.dateText, t.amount);
    if (existing[key]) {
      existing[key]--;            // already in the sheet - consume one
    } else {
      // Amount written as text ("RM 1,234.56") to match how the existing rows
      // in these tabs are stored - a mixed number/text column would look wrong
      // to volunteers and break any formatting they rely on.
      toAdd.push([t.dateText, t.desc1, t.desc2, t.beneficiary, t.account, formatAmount_(t.amount)]);
    }
  });

  if (toAdd.length) {
    sheet.getRange(lastRow + 1, 1, toAdd.length, 6).setValues(toAdd);
  }
  return toAdd.length;
}

/** Comparison key: bank-controlled fields only (see appendNewRows_). */
function rowKey_(dateVal, amount) {
  var d = normDate_(dateVal);
  var a = normAmount_(amount);
  if (!d || a === null) return '';
  return d + '|' + a.toFixed(2);
}

/** Dates may be a real Date, "28 Jul 2026", or "2026-07-28" - normalise all. */
function normDate_(v) {
  if (v instanceof Date) {
    return Utilities.formatDate(v, Session.getScriptTimeZone(), 'yyyy-MM-dd');
  }
  var s = clean_(v);
  if (!s) return '';
  var m = s.match(/^(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})$/);
  if (m) {
    var mm = monthNum_(m[2]);
    if (!mm) return '';
    return m[3] + '-' + pad2_(mm) + '-' + pad2_(parseInt(m[1], 10));
  }
  m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (m) return m[1] + '-' + pad2_(parseInt(m[2], 10)) + '-' + pad2_(parseInt(m[3], 10));
  return '';
}

function normAmount_(v) {
  if (typeof v === 'number') return v;
  var s = String(v === null || v === undefined ? '' : v).replace(/[^0-9.\-]/g, '');
  if (!s || s === '.' || s === '-') return null;
  var n = parseFloat(s);
  return isNaN(n) ? null : n;
}

function toAmount_(v) {
  var n = normAmount_(v);
  return n === null ? 0 : n;
}

function toYyyymm_(dateText) {
  var iso = normDate_(dateText);
  return iso ? iso.substring(0, 4) + iso.substring(5, 7) : '';
}

/**
 * Find the tab for a month, tolerating stray spaces in tab names -
 * the August tab is currently named " 202608" with a leading space,
 * and an exact-match lookup would silently miss it.
 */
function findMonthSheet_(ss, yyyymm) {
  var sheets = ss.getSheets();
  for (var i = 0; i < sheets.length; i++) {
    if (sheets[i].getName().replace(/\s+/g, '') === yyyymm) return sheets[i];
  }
  return null;
}

function clean_(v) {
  return String(v === null || v === undefined ? '' : v).replace(/[\t\r\n]/g, ' ').trim();
}

function monthNum_(abbr) {
  var months = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'];
  var idx = months.indexOf(abbr.toLowerCase());
  return idx === -1 ? 0 : idx + 1;
}

function pad2_(n) { return (n < 10 ? '0' : '') + n; }

/** "RM 1,234.56" - the format the existing sheet rows use. */
function formatAmount_(n) {
  var fixed = n.toFixed(2);
  var parts = fixed.split('.');
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return 'RM ' + parts[0] + '.' + parts[1];
}

function log_(msg) {
  Logger.log(msg);
  console.log(msg);
}

/** Run this ONCE to schedule the import every morning. */
function createDailyTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'importMaybankCsv') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('importMaybankCsv').timeBased().atHour(7).everyDays(1).create();
  Logger.log('Daily import scheduled for around 7am.');
}

/**
 * Safe preview: reports what WOULD be added, without writing anything.
 * Run this first if you want to see the effect before letting it write.
 */
function dryRunImport() {
  var ss = SpreadsheetApp.openById(SHEET_ID);
  var folders = DriveApp.getFoldersByName(CSV_FOLDER_NAME);
  if (!folders.hasNext()) throw new Error('Drive folder not found: ' + CSV_FOLDER_NAME);

  var cutoff = new Date(new Date().getTime() - LOOKBACK_DAYS * 24 * 60 * 60 * 1000);
  var txns = [];
  var it = folders.next().getFiles();
  while (it.hasNext()) {
    var f = it.next();
    if (f.getName().toLowerCase().indexOf('.csv') === -1) continue;
    if (f.getLastUpdated() < cutoff) continue;
    txns = txns.concat(parseMaybankCsv_(f.getBlob().getDataAsString()));
  }

  var byMonth = {};
  txns.forEach(function (t) { (byMonth[t.yyyymm] = byMonth[t.yyyymm] || []).push(t); });

  Object.keys(byMonth).sort().forEach(function (ym) {
    var sheet = findMonthSheet_(ss, ym);
    if (!sheet) { log_(ym + ': NO TAB - ' + byMonth[ym].length + ' row(s) would be skipped'); return; }

    var lastRow = sheet.getLastRow();
    var existing = {};
    if (lastRow > 1) {
      sheet.getRange(2, 1, lastRow - 1, 6).getValues().forEach(function (v) {
        var k = rowKey_(v[0], v[5]);
        if (k) existing[k] = (existing[k] || 0) + 1;
      });
    }
    var n = 0, sample = [];
    byMonth[ym].forEach(function (t) {
      var k = rowKey_(t.dateText, t.amount);
      if (existing[k]) { existing[k]--; }
      else { n++; if (sample.length < 5) sample.push(t.dateText + '  ' + t.beneficiary + '  ' + t.amount); }
    });
    log_(ym + ': would add ' + n + ' of ' + byMonth[ym].length + ' row(s)');
    sample.forEach(function (s) { log_('    ' + s); });
  });
  log_('Dry run only - nothing was written.');
}
