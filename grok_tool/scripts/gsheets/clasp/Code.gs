/**
 * REG → Google Sheet tabs "grok" + "heygen" + "capcut"
 *
 * grok/heygen:
 *   # | Email | Password | Sub2API/Status | Thời gian | VPN
 * capcut:
 *   # | Email | Password | Chỗ đọc mail | Offer đang có | Ngày giờ | Ngày reg
 *
 * POST body.tab = "grok" | "heygen" | "capcut" (mặc định grok).
 * Không đổi tên file, không xóa tab tool khác.
 */

// Set in Apps Script project settings / Script Properties, or edit locally (do not commit real value)
var SECRET = PropertiesService.getScriptProperties().getProperty('WEBAPP_SECRET') || 'CHANGE_ME';
var DEFAULT_GID = 0;
var TAB_NAME = 'grok';
var DEFAULT_PASS = '';

function doPost(e) {
  try {
    var body = {};
    if (e && e.postData && e.postData.contents) {
      body = JSON.parse(e.postData.contents);
    }
    if ((body.secret || '') !== SECRET) {
      return jsonOut_({ ok: false, error: 'bad secret' });
    }
    // action=peek | status → đọc sheet (F5 check), không ghi
    var action = String(body.action || 'write').toLowerCase();
    if (action === 'peek' || action === 'status' || action === 'check') {
      return jsonOut_({ ok: true, result: peekTab_(body) });
    }
    if (action === 'ensure_tab') {
      return jsonOut_({ ok: true, result: ensureTab_(body) });
    }
    if (action === 'append') {
      return jsonOut_({ ok: true, result: appendAccount_(body) });
    }
    return jsonOut_({ ok: true, result: writePayload_(body) });
  } catch (err) {
    return jsonOut_({ ok: false, error: String(err) });
  }
}

function doGet(e) {
  try {
    var p = (e && e.parameter) || {};
    if (p.secret === SECRET && (p.action === 'peek' || p.action === 'status')) {
      return jsonOut_({ ok: true, result: peekTab_({}) });
    }
  } catch (err) {
    return jsonOut_({ ok: false, error: String(err) });
  }
  return jsonOut_({
    ok: true,
    msg: 'Grok success ledger. POST write | POST/GET action=peek&secret=... for status.',
  });
}

function tabNameOf_(body) {
  var t = String((body && (body.tab || body.tab_name)) || '').trim().toLowerCase();
  if (t === 'heygen') return 'heygen';
  if (t === 'capcut') return 'capcut';
  return 'grok';
}

function titleOf_(tabName) {
  if (tabName === 'heygen') return 'HEYGEN REG  ·  ACC THÀNH CÔNG';
  if (tabName === 'capcut') return 'CAPCUT REG  ·  ACC THÀNH CÔNG';
  return 'GROK REG  ·  ACC THÀNH CÔNG';
}

function nameColOf_(tabName) {
  return tabName === 'heygen' ? 'Status' : 'Sub2API Name';
}

function colCountOf_(tabName) {
  return tabName === 'capcut' ? 7 : 6;
}

function headerOf_(tabName) {
  if (tabName === 'capcut') {
    return ['#', 'Email', 'Password', 'Chỗ đọc mail', 'Offer đang có', 'Ngày giờ', 'Ngày reg'];
  }
  return ['#', 'Email', 'Password', nameColOf_(tabName), 'Thời gian', 'VPN'];
}

function bannerOf_(tabName, n) {
  if (tabName === 'capcut') {
    return 'FULL  (email | mk | chỗ đọc mail | offer | ngày giờ | ngày reg)  ·  ' + n + ' acc';
  }
  var mid = tabName === 'heygen' ? 'status' : 'sub2api_name';
  return 'FULL  (email | pass | ' + mid + ' | thời gian | VPN)  ·  ' + n + ' acc';
}

function getOrCreateTab_(ss, tabName, gid) {
  var byName = ss.getSheetByName(tabName);
  if (byName) return byName;
  if (tabName === 'grok' && gid) {
    var byGid = sheetByGid_(ss, gid);
    if (byGid) {
      try { byGid.setName('grok'); } catch (eN) {}
      return byGid;
    }
  }
  return ss.insertSheet(tabName);
}

function ensureTab_(body) {
  var ss = (body && body.spreadsheet_id)
    ? SpreadsheetApp.openById(body.spreadsheet_id)
    : SpreadsheetApp.getActiveSpreadsheet();
  var tabName = tabNameOf_(body);
  var gid = parseInt((body && body.gid) || DEFAULT_GID, 10);
  var dash = getOrCreateTab_(ss, tabName, gid);
  if (dash.getLastRow() === 0) {
    writeEmptyLayout_(dash, tabName, {});
  }
  SpreadsheetApp.flush();
  return {
    tab: dash.getName(),
    tab_id: dash.getSheetId(),
    created_or_ok: true,
  };
}

function findHeaderRow_(values) {
  for (var i = 0; i < values.length; i++) {
    var c0 = String(values[i][0] || '').trim();
    var c1 = String(values[i][1] || '').toLowerCase();
    if (c0 === '#' && c1.indexOf('email') >= 0) return i;
  }
  return -1;
}

/** Mỗi acc thành công → thêm/cập nhật 1 dòng, không ghi đè cả bảng. */
function appendAccount_(body) {
  var ss = (body && body.spreadsheet_id)
    ? SpreadsheetApp.openById(body.spreadsheet_id)
    : SpreadsheetApp.getActiveSpreadsheet();
  var tabName = tabNameOf_(body);
  var gid = parseInt((body && body.gid) || DEFAULT_GID, 10);
  var dash = getOrCreateTab_(ss, tabName, gid);
  var acc = body.account || {};
  var email = String(acc.email || '').trim();
  if (!email || email.indexOf('@') < 0) {
    throw new Error('append missing email');
  }
  var pass = String(acc.password || '');
  var name = String(acc.name || acc.sub2api || acc.status || '');
  var when = String(acc.time || acc.ts || '');
  var vpn = String(acc.vpn || '—');
  var inbox = String(acc.mail_inbox || acc.inbox || acc.mail || '');
  var offer = String(acc.offer || acc.offer_label || name || '');
  var now = Utilities.formatDate(new Date(), 'Asia/Ho_Chi_Minh', 'yyyy-MM-dd HH:mm:ss');
  if (!when) when = now;
  var regDate = String(acc.reg_date || acc.date || '').trim();
  if (!regDate) regDate = when.split(' ')[0] || now.split(' ')[0];
  var ncols = colCountOf_(tabName);

  if (dash.getLastRow() === 0) {
    writeEmptyLayout_(dash, tabName, body.summary || {});
  }

  try {
    var oldF = dash.getFilter();
    if (oldF) oldF.remove();
  } catch (eF0) {}

  var lastRow = Math.max(dash.getLastRow(), 1);
  var lastCol = Math.max(dash.getLastColumn(), ncols);
  var values = dash.getRange(1, 1, lastRow, lastCol).getDisplayValues();
  var headerIdx = findHeaderRow_(values);
  if (headerIdx < 0) {
    writeEmptyLayout_(dash, tabName, body.summary || {});
    lastRow = Math.max(dash.getLastRow(), 1);
    values = dash.getRange(1, 1, lastRow, lastCol).getDisplayValues();
    headerIdx = findHeaderRow_(values);
  }
  if (headerIdx < 0) throw new Error('no header row');

  var found = -1;
  var count = 0;
  for (var r = headerIdx + 1; r < values.length; r++) {
    var em = String(values[r][1] || '');
    if (!em || em.indexOf('@') < 0) {
      if (!String(values[r][0] || '').trim() && !em) break;
      continue;
    }
    count++;
    if (em.toLowerCase() === email.toLowerCase()) found = r;
  }

  var n = found >= 0 ? values[found][0] : count + 1;
  var rowVals = tabName === 'capcut'
    ? [n, email, pass, inbox || '—', offer || '—', when || '—', regDate || '—']
    : [n, email, pass, name || '—', when || '—', vpn || '—'];
  var added = false;
  if (found >= 0) {
    dash.getRange(found + 1, 1, 1, ncols).setValues([rowVals]);
  } else {
    var dest = headerIdx + 1 + count + 1;
    dash.getRange(dest, 1, 1, ncols).setValues([rowVals]);
    count++;
    added = true;
  }

  try { dash.getRange(2, 2, 1, 2).merge().setValue(now); } catch (eU) {}
  try { dash.getRange(3, 2).setValue(count); } catch (eT) {}
  if (headerIdx > 0) {
    try {
      dash.getRange(headerIdx, 1, 1, ncols).merge()
        .setValue(bannerOf_(tabName, count))
        .setFontWeight('bold').setBackground('#e8f0fe');
    } catch (eB) {}
  }
  try {
    dash.getRange(headerIdx + 1, 1, Math.max(count, 1) + 1, ncols).createFilter();
  } catch (eF) {}

  SpreadsheetApp.flush();
  return {
    tab: dash.getName(),
    tab_id: dash.getSheetId(),
    email: email,
    added: added,
    full: count,
    updated: now,
  };
}

/** Đọc tab grok/heygen — dùng để agent tự check, không mở browser. */
function peekTab_(body) {
  var ss = (body && body.spreadsheet_id)
    ? SpreadsheetApp.openById(body.spreadsheet_id)
    : SpreadsheetApp.getActiveSpreadsheet();

  var tabs = ss.getSheets().map(function (sh) {
    return { name: sh.getName(), id: sh.getSheetId(), rows: sh.getLastRow() };
  });
  var tabName = tabNameOf_(body);
  var gidPeek = parseInt((body && body.gid) || DEFAULT_GID, 10);
  var dash =
    ss.getSheetByName(tabName) ||
    (tabName === 'grok' ? sheetByGid_(ss, gidPeek) : null) ||
    ss.getSheets()[0];
  var lastRow = dash.getLastRow();
  var lastCol = Math.max(dash.getLastColumn(), 4);
  var values =
    lastRow > 0 ? dash.getRange(1, 1, lastRow, lastCol).getDisplayValues() : [];

  // Find header row with Email
  var headerIdx = -1;
  for (var i = 0; i < values.length; i++) {
    var c0 = String(values[i][0] || '').trim();
    var c1 = String(values[i][1] || '').toLowerCase();
    if (c0 === '#' && c1.indexOf('email') >= 0) {
      headerIdx = i;
      break;
    }
  }
  var accRows = [];
  if (headerIdx >= 0) {
    for (var r = headerIdx + 1; r < values.length; r++) {
      var email = String(values[r][1] || '');
      if (!email || email.indexOf('@') < 0) {
        // stop at blank / OPS section
        if (!String(values[r][0] || '').trim() && !email) break;
        if (String(values[r][0] || '').indexOf('OPS') >= 0) break;
        continue;
      }
      accRows.push({
        n: values[r][0],
        email: values[r][1],
        password: values[r][2],
        sub2api: values[r][3],
        time: values[r][4] || '',
        vpn: values[r][5] || '',
      });
    }
  }

  return {
    file_name: ss.getName(),
    tab: dash.getName(),
    tab_id: dash.getSheetId(),
    all_tabs: tabs,
    last_row: lastRow,
    full_count: accRows.length,
    head: values.slice(0, 8),
    first_acc: accRows.slice(0, 3),
    last_acc: accRows.slice(-3),
    all_acc: accRows.length <= 200 ? accRows : accRows.slice(0, 100).concat(accRows.slice(-20)),
    has_fail_emails: values.some(function (row) {
      var t = row.join(' ').toLowerCase();
      return t.indexOf('pqj6ddftuh') >= 0 || t.indexOf('hujdohqtoa') >= 0;
    }),
  };
}

function writePayload_(body) {
  var ss = SpreadsheetApp.openById(
    body.spreadsheet_id || SpreadsheetApp.getActiveSpreadsheet().getId()
  );
  var sIn = body.summary || {};
  var accounts = body.accounts || [];

  // FULL only — columns: # Email Pass Sub2API Thời gian VPN
  // payload row: [# Tag Email Pass Sub2 Status Date Exported VPN]
  var full = [];
  for (var i = 0; i < accounts.length; i++) {
    var a = accounts[i];
    var tag = String(a[1] || 'FULL').toUpperCase();
    if (tag === 'REG' || tag === 'FAIL') continue;
    var email = a[2] || '';
    var pass = a[3] || '';
    var sub2 = a[4] || '';
    var when = a[7] || a[6] || '';
    var vpn = a[8] || '';
    if (String(email).indexOf('@') < 0 && String(a[0] || '').indexOf('@') >= 0) {
      // alternate layout
      email = a[0]; pass = a[1]; sub2 = a[2]; when = a[3] || ''; vpn = a[4] || '';
    }
    full.push([
      full.length + 1,
      email,
      pass,
      sub2,
      when || '—',
      vpn || '—',
    ]);
  }

  var s = {
    exported_at: sIn.exported_at || Utilities.formatDate(new Date(), 'Asia/Ho_Chi_Minh', 'yyyy-MM-dd HH:mm:ss'),
    batch_label: sIn.batch_label || '',
    batch_full: sIn.acc_full != null ? sIn.acc_full : '',
    batch_fail: sIn.acc_fail != null ? sIn.acc_fail : '',
    batch_rate: sIn.ok_rate != null ? sIn.ok_rate : '',
    total_full: sIn.alltime_full != null ? sIn.alltime_full : full.length,
    password: sIn.password_common || DEFAULT_PASS,
    vpn_label: sIn.vpn_label || '—',
    vpn_country: sIn.vpn_country || '',
    vpn_ip: sIn.vpn_ip || '',
  };
  if (!s.total_full) s.total_full = full.length;

  var tabName = tabNameOf_(body);
  var gid = parseInt(body.gid || DEFAULT_GID, 10);
  var dash = getOrCreateTab_(ss, tabName, gid);
  writeLayout_(dash, tabName, s, full);

  deleteOtherTabs_(ss, dash);
  try { ss.setActiveSheet(dash); } catch (eA) {}
  SpreadsheetApp.flush();

  return {
    full: full.length,
    total_full: s.total_full,
    tab: dash.getName(),
    tab_id: dash.getSheetId(),
    updated: s.exported_at,
  };
}

function writeEmptyLayout_(dash, tabName, sIn) {
  writeLayout_(dash, tabName, {
    exported_at: (sIn && sIn.exported_at) || Utilities.formatDate(new Date(), 'Asia/Ho_Chi_Minh', 'yyyy-MM-dd HH:mm:ss'),
    batch_label: '',
    batch_full: '',
    password: (sIn && sIn.password) || DEFAULT_PASS,
    vpn_label: (sIn && sIn.vpn_label) || '—',
    total_full: 0,
  }, []);
}

function writeLayout_(dash, tabName, s, full) {
  full = full || [];
  var ncols = colCountOf_(tabName);
  try {
    var oldFilter = dash.getFilter();
    if (oldFilter) oldFilter.remove();
  } catch (eFilter0) {}
  try { dash.clear(); } catch (eClear) { dash.clearContents(); }
  try { dash.clearFormats(); } catch (eFmt) {}

  dash.getRange(1, 1, 1, ncols).merge()
    .setValue(titleOf_(tabName))
    .setFontWeight('bold').setFontSize(16).setBackground('#00C8D2').setFontColor('#fff')
    .setVerticalAlignment('middle');
  if (tabName !== 'capcut') {
    dash.getRange(1, 1, 1, ncols).setBackground('#1a73e8');
  }
  dash.setRowHeight(1, 36);

  dash.getRange(2, 1).setValue('Cập nhật').setFontWeight('bold').setBackground('#e8f0fe');
  dash.getRange(2, 2, 1, 2).merge().setValue(s.exported_at || '');
  dash.getRange(3, 1).setValue('Tổng FULL').setFontWeight('bold').setBackground('#e8f0fe');
  dash.getRange(3, 2).setValue(s.total_full != null ? s.total_full : full.length).setFontWeight('bold').setFontSize(14);
  dash.getRange(3, 3).setValue('Pass chung').setFontWeight('bold').setBackground('#e8f0fe');
  dash.getRange(3, 4).setValue(s.password || '');

  dash.getRange(4, 1).setValue('VPN / IP').setFontWeight('bold').setBackground('#fef7e0');
  dash.getRange(4, 2, 1, 3).merge().setValue(s.vpn_label || '—').setFontWeight('bold');

  var row = 6;
  if (s.batch_label || s.batch_full !== '') {
    dash.getRange(row, 1).setValue('Lần export này').setFontWeight('bold').setBackground('#e6f4ea');
    var batchTxt = s.batch_label || '';
    if (s.batch_full !== '') {
      batchTxt += (batchTxt ? '  ·  ' : '') +
        'FULL=' + s.batch_full +
        (s.batch_fail !== '' ? '  FAIL=' + s.batch_fail : '') +
        (s.batch_rate !== '' ? '  RATE=' + s.batch_rate + '%' : '');
    }
    dash.getRange(row, 2, 1, 3).merge().setValue(batchTxt);
    row = 8;
  }

  dash.getRange(row, 1, 1, ncols).merge()
    .setValue(bannerOf_(tabName, full.length))
    .setFontWeight('bold').setBackground('#e8f0fe');
  row++;
  dash.getRange(row, 1, 1, ncols).setValues([headerOf_(tabName)])
    .setFontWeight('bold').setBackground('#d2e3fc');
  var headerRow = row;
  row++;
  if (full.length) {
    dash.getRange(row, 1, full.length, ncols).setValues(full);
    try {
      var oldF2 = dash.getFilter();
      if (oldF2) oldF2.remove();
    } catch (eF) {}
    try {
      dash.getRange(headerRow, 1, full.length + 1, ncols).createFilter();
    } catch (eF2) {}
  } else {
    dash.getRange(row, 1).setValue('(chua co acc FULL)');
  }

  dash.setColumnWidth(1, 50);
  dash.setColumnWidth(2, 300);
  dash.setColumnWidth(3, 180);
  if (tabName === 'capcut') {
    dash.setColumnWidth(4, 280);
    dash.setColumnWidth(5, 280);
    dash.setColumnWidth(6, 170);
    dash.setColumnWidth(7, 120);
  } else {
    dash.setColumnWidth(4, 150);
    dash.setColumnWidth(5, 170);
    dash.setColumnWidth(6, 220);
  }
  dash.setFrozenRows(headerRow);
}

function deleteOtherTabs_(ss, keep) {
  var sheets = ss.getSheets();
  for (var i = sheets.length - 1; i >= 0; i--) {
    var sh = sheets[i];
    if (sh.getSheetId() === keep.getSheetId()) continue;
    var n = sh.getName();
    if (n === 'capcut' || n === 'heygen' || n === 'grok') continue;
    if (
      n === 'Acc FULL' || n === 'Acc FAIL' || n === 'Lich su' ||
      n === 'Tong quan' || n.indexOf('grok_old') === 0 || n.indexOf('old_') === 0
    ) {
      try {
        if (ss.getSheets().length > 1) ss.deleteSheet(sh);
      } catch (eDel) {}
    }
  }
}

function sheetByGid_(ss, gid) {
  var sheets = ss.getSheets();
  for (var i = 0; i < sheets.length; i++) {
    if (sheets[i].getSheetId() === gid) return sheets[i];
  }
  return null;
}

function jsonOut_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
