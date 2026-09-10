'use strict';

const ExcelJS = require('exceljs');

const REGION_LABELS = Object.freeze({
  'ap-northeast-1': '东京',
  'ap-northeast-2': '首尔',
  'ap-northeast-3': '大阪',
  'ap-east-1': '香港',
  'ap-southeast-1': '新加坡',
  'ap-southeast-2': '悉尼',
  'ap-south-1': '孟买',
  'eu-central-1': '法兰克福',
  'eu-west-1': '爱尔兰',
  'us-east-1': '美国东部（弗吉尼亚北部）',
  'us-east-2': '美国东部（俄亥俄）',
  'us-west-1': '美国西部（加利福尼亚北部）',
  'us-west-2': '美国西部（俄勒冈）',
  global: '全球',
});

const HEADER_FILL = 'FF16576B';
const HEADER_TEXT = 'FFFFFFFF';
const LABEL_FILL = 'FFEAF2F5';
const ALT_FILL = 'FFF5F9FA';
const BORDER = 'FFD4DDE1';
const TEXT = 'FF24313A';
const PROVIDER_SCENARIO_LABELS = Object.freeze({
  aws: {
    on_demand: '按需月费',
    one_year_commitment: '1 年预留折合月费',
    three_year_commitment: '3 年预留折合月费',
  },
  azure: {
    on_demand: '即用即付月费',
    one_year_commitment: '1 年预留折合月费',
    three_year_commitment: '3 年预留折合月费',
  },
  oci: { on_demand: 'OCI 公开按量月费' },
  gcp: {
    on_demand: '按需月费',
    one_year_commitment: '1 年承诺使用折合月费',
    three_year_commitment: '3 年承诺使用折合月费',
  },
});

function scenarioLabel(record, scenario) {
  return PROVIDER_SCENARIO_LABELS[record.cloud_provider || 'aws']?.[scenario.scenario_key]
    || scenario.label
    || null;
}

function friendlyRegion(value) {
  const code = String(value || '').trim();
  return REGION_LABELS[code] || code || '-';
}

function simplifyCustomerText(value) {
  const normalized = String(value ?? '')
    .replace(/最低合法值/g, '最低可用值')
    .replace(/\s{2,}/g, ' ')
    .trim();
  if (!normalized) return '';

  const endedWithFullStop = /[。；;]$/.test(normalized);
  const clauses = normalized
    .split(/[；;。]+/)
    .map((rawClause) => rawClause.trim())
    .filter(Boolean)
    .map((rawClause) => rawClause
      .replace(/[，,]?\s*(?:在|从)[^，,]*候选[^；;。]*?(?:月费|价格|费用|成本)(?:最低|较低|最便宜)[^；;。]*$/g, '')
      .replace(/[，,]\s*(?:并|且)?按官方候选价格选择(?:较低档|最低价[^，,]*)/g, '')
      .replace(/按不超配规则(?:选择|选)?(?:最临近的?)?(?:小一档|较低档)\s*/g, '')
      .replace(/^.*?按官方较低价格选择\s*/g, '')
      .replace(/最低成本/g, '')
      .replace(/低成本/g, '')
      .replace(/官方最低价/g, '官方价格')
      .replace(/最低价(?:格)?/g, '')
      .replace(/按官方允许的\s*/g, '采用 ')
      .replace(/[，,]\s*(?:并|且)?(?:价格|月费|费用|成本)(?:最低|较低)[^，,]*$/g, '')
      .replace(/\s{2,}/g, ' ')
      .replace(/[，,\s]+$/g, '')
      .trim())
    .filter((clause) => !(
      /(?:候选|筛选|比价|选型)/.test(clause)
      && /(?:月费|价格|费用|成本)/.test(clause)
      && /(?:最低|较低|最便宜)/.test(clause)
    ))
    .filter((clause) => !(
      /(?:本次查到|官方候选|候选型号|候选筛选|筛选过程|内部选型|比价)/.test(clause)
      && /(?:价格|月费|费用|成本|总价|更低|最低)/.test(clause)
    ))
    .filter((clause) => !(
      /(?:未查到|未返回|没有对应|无对应)/.test(clause)
      && /(?:价格|费用|费率|预留|承诺)/.test(clause)
    ))
    .filter((clause) => !(
      /(?:继续|回退|沿用)/.test(clause)
      && /(?:按需|即用即付|按量|预留|承诺)/.test(clause)
    ))
    .filter((clause) => !/(?:不再|无需|没有|未)(?:重复)?加价/.test(clause))
    .filter(Boolean);
  const result = clauses.join('；');
  if (!result) return '';
  return endedWithFullStop ? `${result}。` : result;
}

function compactPart(value) {
  return simplifyCustomerText(value)
    .replace(/^(?:客户(?:原)?需求|客户要求|原需求|报价配置|最终配置|本次配置)[:：]\s*/g, '')
    .replace(/[。；;，,\s]+$/g, '')
    .trim();
}

function conciseReason(value) {
  const reason = compactPart(value);
  if (!reason) return '';
  if (/没有完全匹配|无完全匹配/.test(reason)) return '无完全匹配规格';
  if (/客户未指定|未提供|未明确/.test(reason)) return '客户未指定';
  const firstClause = reason.split(/[；;。]/, 1)[0]
    .replace(/(?:因此|所以)?(?:改为|采用|使用|选择).*$/g, '')
    .replace(/以满足需求/g, '')
    .replace(/[，,\s]+$/g, '')
    .trim();
  return firstClause.slice(0, 36);
}

function materialAdjustment(item) {
  const requirement = compactPart(item?.customer_requirement);
  const configuration = compactPart(item?.quoted_configuration);
  if (!requirement || !configuration || requirement === configuration) return '';
  const reason = conciseReason(item?.reason);
  return `${requirement} → ${configuration}${reason ? `（${reason}）` : ''}`;
}

function componentDetails(record) {
  const components = [
    ...(record.resource_ir || []),
    ...(record.zero_cost_ir || []),
  ];
  const lineItemsByComponent = new Map(
    (record.verification?.costs?.line_items || [])
      .filter((item) => item.component_key)
      .map((item) => [item.component_key, Number(item.monthly)]),
  );
  const adjustmentsByComponent = new Map();
  for (const item of record.adjustments || []) {
    const note = materialAdjustment(item);
    if (!note) continue;
    const existing = adjustmentsByComponent.get(item.component_key) || [];
    existing.push(note);
    adjustmentsByComponent.set(item.component_key, existing);
  }
  const scenarios = Array.isArray(record.pricing_scenarios)
    ? record.pricing_scenarios.filter((scenario) => scenarioLabel(record, scenario))
    : [];
  return components.map((component, index) => {
    const display = component.customer_facing || {};
    const directMonthly = Number(
      component.expected_monthly_cost ?? component.monthly_cost,
    );
    const fallbackMonthly = lineItemsByComponent.get(component.component_key);
    const monthly = Number.isFinite(directMonthly) ? directMonthly : fallbackMonthly;
    const scenarioCosts = new Map(
      (component.scenario_costs || []).map((cost) => [cost.scenario_key, Number(cost.monthly_cost)]),
    );
    const priceCells = scenarios.length > 0
      ? scenarios.map((scenario) => (
        component.pricing_basis === 'official_no_additional_charge'
          ? 0
          : scenarioCosts.get(scenario.scenario_key)
      ))
      : [monthly];
    return [
      index + 1,
      simplifyCustomerText(
        display.service_name || component.component_key,
      ),
      friendlyRegion(component.region || record.default_region),
      simplifyCustomerText(display.model_or_plan || component.instance || '官方方案'),
      simplifyCustomerText(display.quantity || '-'),
      simplifyCustomerText(display.configuration_summary || '-'),
      ...priceCells.map((value) => (Number.isFinite(value) ? value : null)),
      simplifyCustomerText(display.reference_unit_price || ''),
      (adjustmentsByComponent.get(component.component_key) || []).join('；') || null,
    ];
  });
}

function applyBorder(cell) {
  cell.border = {
    top: { style: 'thin', color: { argb: BORDER } },
    left: { style: 'thin', color: { argb: BORDER } },
    bottom: { style: 'thin', color: { argb: BORDER } },
    right: { style: 'thin', color: { argb: BORDER } },
  };
}

async function buildQuoteWorkbook(record) {
  const allowedStatuses = new Set(['official_price_verified']);
  if (!allowedStatuses.has(record.verification?.status)) {
    const error = new Error('Only an official-price-verified quote can be rendered.');
    error.code = 'quote_not_verified';
    throw error;
  }

  const costs = record.verification.costs || {};
  const scenarios = Array.isArray(record.pricing_scenarios)
    ? record.pricing_scenarios.filter((scenario) => scenarioLabel(record, scenario))
    : [];
  const rows = componentDetails(record);
  const priceColumnCount = scenarios.length || 1;
  if (rows.some((row) => row.slice(6, 6 + priceColumnCount).some((value) => !Number.isFinite(value)))) {
    const error = new Error('Every quote component must have a verified monthly cost.');
    error.code = 'component_monthly_cost_required';
    throw error;
  }

  const workbook = new ExcelJS.Workbook();
  workbook.creator = 'AstraQuote';
  workbook.created = new Date(record.verification.verified_at || Date.now());
  workbook.calcProperties.fullCalcOnLoad = true;
  const sheet = workbook.addWorksheet('报价单', {
    properties: { defaultRowHeight: 21 },
    views: [{ state: 'frozen', ySplit: 1, showGridLines: false }],
    pageSetup: {
      orientation: 'landscape',
      fitToPage: true,
      fitToWidth: 1,
      fitToHeight: 0,
      paperSize: 9,
      margins: { left: 0.25, right: 0.25, top: 0.35, bottom: 0.35, header: 0.1, footer: 0.1 },
    },
  });

  sheet.columns = [
    { width: 7 },
    { width: 25 },
    { width: 14 },
    { width: 24 },
    { width: 11 },
    { width: 54 },
    ...Array.from({ length: priceColumnCount }, () => ({ width: 18 })),
    { width: 22 },
    { width: 42 },
  ];

  const priceHeaders = scenarios.length > 0
    ? scenarios.map((scenario) => scenarioLabel(record, scenario))
    : ['月费'];
  const headers = ['序号', '云服务', '区域', '型号 / 方案', '数量', '配置', ...priceHeaders, '参考单价', '备注'];
  const headerRow = sheet.getRow(1);
  headerRow.values = headers;
  headerRow.height = 27;
  headerRow.eachCell((cell) => {
    cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: HEADER_FILL } };
    cell.font = { bold: true, color: { argb: HEADER_TEXT }, name: 'Arial' };
    cell.alignment = { horizontal: 'center', vertical: 'middle', wrapText: true };
    applyBorder(cell);
  });

  const firstDataRow = 2;
  for (const values of rows) {
    const row = sheet.addRow(values);
    row.alignment = { vertical: 'top', wrapText: true };
    row.eachCell((cell) => {
      cell.font = { color: { argb: TEXT }, size: 10, name: 'Arial' };
      if (row.number % 2 === 1) {
        cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: ALT_FILL } };
      }
      applyBorder(cell);
    });
    row.getCell(1).alignment = { horizontal: 'center', vertical: 'top' };
    row.getCell(5).alignment = { horizontal: 'center', vertical: 'top', wrapText: true };
    for (let column = 7; column < 7 + priceColumnCount; column += 1) {
      row.getCell(column).numFmt = '"$"#,##0.00';
      row.getCell(column).alignment = { horizontal: 'right', vertical: 'top' };
    }
  }

  const lastDataRow = firstDataRow + rows.length - 1;
  const monthlyTotals = scenarios.length > 0
    ? scenarios.map((scenario) => Number(scenario.monthly_total))
    : [Number(costs.monthly || 0)];
  const totalRow = sheet.addRow([
    scenarios.length > 0 ? '月费合计' : '合计',
    null, null, null, null, null,
    ...monthlyTotals.map((total, index) => ({
      formula: rows.length
        ? `SUM(${sheet.getColumn(7 + index).letter}${firstDataRow}:${sheet.getColumn(7 + index).letter}${lastDataRow})`
        : '0',
      result: total,
    })),
    null,
    null,
  ]);
  sheet.mergeCells(`A${totalRow.number}:F${totalRow.number}`);
  totalRow.height = 25;
  totalRow.eachCell({ includeEmpty: true }, (cell) => {
    cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: LABEL_FILL } };
    cell.font = { bold: true, color: { argb: TEXT }, name: 'Arial' };
    cell.alignment = { vertical: 'middle', wrapText: true };
    applyBorder(cell);
  });
  for (let column = 7; column < 7 + priceColumnCount; column += 1) {
    totalRow.getCell(column).numFmt = '"$"#,##0.00';
  }

  let lastPrintableRow = totalRow.number;
  if (scenarios.length > 0) {
    const upfrontRow = sheet.addRow([
      '预付费合计',
      null, null, null, null, null,
      ...scenarios.map((scenario) => Number(scenario.upfront_total || 0)),
      null,
      null,
    ]);
    sheet.mergeCells(`A${upfrontRow.number}:F${upfrontRow.number}`);
    upfrontRow.height = 25;
    upfrontRow.eachCell({ includeEmpty: true }, (cell) => {
      cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: LABEL_FILL } };
      cell.font = { bold: true, color: { argb: TEXT }, name: 'Arial' };
      cell.alignment = { vertical: 'middle', wrapText: true };
      applyBorder(cell);
    });
    for (let column = 7; column < 7 + priceColumnCount; column += 1) {
      upfrontRow.getCell(column).numFmt = '"$"#,##0.00';
    }
    lastPrintableRow = upfrontRow.number;
  }

  const lastColumn = 8 + priceColumnCount;
  sheet.autoFilter = { from: { row: 1, column: 1 }, to: { row: lastDataRow || 1, column: lastColumn } };
  sheet.pageSetup.printArea = `A1:${sheet.getColumn(lastColumn).letter}${lastPrintableRow}`;
  return workbook.xlsx.writeBuffer().then((buffer) => Buffer.from(buffer));
}

module.exports = {
  buildQuoteWorkbook,
  componentDetails,
  conciseReason,
  friendlyRegion,
  materialAdjustment,
  simplifyCustomerText,
};
