'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const ExcelJS = require('exceljs');

const {
  buildQuoteWorkbook,
  componentDetails,
  friendlyRegion,
  materialAdjustment,
  simplifyCustomerText,
} = require('../lib/quote-workbook');

function verifiedRecord() {
  return {
    quote_id: 'aqv2_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    quote_name: '东京业务系统 AWS 报价',
    default_region: 'ap-northeast-1',
    currency: 'USD',
    adjustments: [{
      component_key: 'cmp_ec2_0001',
      customer_requirement: '内存 23 GiB',
      quoted_configuration: '内存 32 GiB',
      reason: 'AWS 没有完全匹配的 23 GiB 规格，向上匹配以满足需求。',
      price_impact: '已包含在 EC2 月费中。',
    }],
    resource_ir: [{
      component_key: 'cmp_ec2_0001',
      region: 'ap-northeast-1',
      instance: 'm7g.xlarge',
      expected_monthly_cost: '245.67',
      customer_facing: {
        service_name: 'Amazon EC2 云服务器',
        model_or_plan: 'm7g.xlarge',
        quantity: '2',
        configuration_summary: 'Linux 按需实例 2 台，每台 32 GiB 内存，每月运行 730 小时。',
        reference_unit_price: '$0.1683/小时',
      },
    }],
    verification: {
      status: 'official_price_verified',
      verified_at: '2026-09-09T00:00:00.000Z',
      costs: {
        upfront: 0,
        monthly: 245.67,
        total_12_months: 2948.04,
        line_items: [{ component_key: 'cmp_ec2_0001', monthly: 245.67 }],
      },
    },
  };
}

async function readWorkbook(record = verifiedRecord()) {
  const buffer = await buildQuoteWorkbook(record);
  const workbook = new ExcelJS.Workbook();
  await workbook.xlsx.load(buffer);
  return { buffer, sheet: workbook.getWorksheet('报价单') };
}

test('creates a compact Excel workbook from verified quote data', async () => {
  const { buffer, sheet } = await readWorkbook();
  assert.ok(Buffer.isBuffer(buffer));
  assert.equal(buffer.subarray(0, 2).toString('ascii'), 'PK');
  assert.equal(sheet.getCell('A1').value, '序号');
  assert.equal(sheet.getCell('G1').value, '月费');
  assert.equal(sheet.getCell('G2').value, 245.67);
  assert.equal(sheet.getCell('A3').value, '合计');
  assert.equal(sheet.getCell('G3').value.result, 245.67);
});

test('omits the large document title and keeps adjustments in the remarks column', async () => {
  const { sheet } = await readWorkbook();
  const allText = [];
  sheet.eachRow((row) => row.eachCell((cell) => allText.push(String(cell.text || ''))));
  assert.equal(allText.includes('AWS 云服务报价单'), false);
  assert.equal(allText.includes('报价名称'), false);
  assert.equal(allText.includes('12个月估算'), false);
  assert.equal(allText.includes('官方报价'), false);
  assert.match(sheet.getCell('I2').value, /内存 23 GiB → 内存 32 GiB/);
  assert.match(sheet.getCell('I2').value, /无完全匹配规格/);
  assert.doesNotMatch(sheet.getCell('I2').value, /费用|本次配置|理解|priceDimensions/);
});

test('renders a structured zero-cost resource as its own zero-dollar row', async () => {
  const record = verifiedRecord();
  record.zero_cost_ir = [{
    component_key: 'cmp_vpc_0001',
    region: 'ap-northeast-1',
    pricing_basis: 'official_no_additional_charge',
    monthly_cost: '0',
    customer_facing: {
      service_name: 'Amazon VPC',
      model_or_plan: 'VPC',
      quantity: '1 个',
      configuration_summary: '普通 VPC 1 个。',
    },
  }];

  const { sheet } = await readWorkbook(record);
  assert.equal(sheet.getCell('B3').value, 'Amazon VPC');
  assert.equal(sheet.getCell('G3').value, 0);
  assert.equal(sheet.getCell('I3').value, null);
  assert.equal(sheet.getCell('A4').value, '合计');
});

test('renders every selected pricing scenario as a separate component column', async () => {
  const record = verifiedRecord();
  record.pricing_scenarios = [
    { scenario_key: 'on_demand', monthly_total: '245.67', upfront_total: '0' },
    { scenario_key: 'one_year_commitment', monthly_total: '183.92', upfront_total: '2207.04' },
    { scenario_key: 'three_year_commitment', monthly_total: '117.58', upfront_total: '4232.88' },
  ];
  record.resource_ir[0].scenario_costs = [
    { scenario_key: 'on_demand', monthly_cost: '245.67', upfront_cost: '0' },
    { scenario_key: 'one_year_commitment', monthly_cost: '183.92', upfront_cost: '2207.04' },
    { scenario_key: 'three_year_commitment', monthly_cost: '117.58', upfront_cost: '4232.88' },
  ];

  const { sheet } = await readWorkbook(record);
  assert.equal(sheet.getCell('G1').value, '按需月费');
  assert.equal(sheet.getCell('H1').value, '1 年预留折合月费');
  assert.equal(sheet.getCell('I1').value, '3 年预留折合月费');
  assert.equal(sheet.getCell('G2').value, 245.67);
  assert.equal(sheet.getCell('H2').value, 183.92);
  assert.equal(sheet.getCell('I2').value, 117.58);
  assert.equal(sheet.getCell('A3').value, '月费合计');
  assert.equal(sheet.getCell('H3').value.result, 183.92);
  assert.equal(sheet.getCell('A4').value, '预付费合计');
  assert.equal(sheet.getCell('I4').value, 4232.88);
});

test('refuses to render an unverified quote', async () => {
  const record = verifiedRecord();
  record.verification.status = 'pending';
  await assert.rejects(buildQuoteWorkbook(record), (error) => error.code === 'quote_not_verified');
});

test('component rows contain a numeric monthly price and a concise change note', () => {
  const rows = componentDetails(verifiedRecord());
  assert.equal(rows.length, 1);
  assert.equal(rows[0].length, 9);
  assert.equal(rows[0][1], 'Amazon EC2 云服务器');
  assert.equal(rows[0][2], '东京');
  assert.equal(rows[0][3], 'm7g.xlarge');
  assert.equal(rows[0][4], '2');
  assert.equal(rows[0][6], 245.67);
  assert.match(rows[0][8], /23 GiB → 内存 32 GiB/);
});

test('unchanged or incomplete adjustments are not shown to customers', () => {
  assert.equal(materialAdjustment({ customer_requirement: '2 核 4G', quoted_configuration: '2 核 4G' }), '');
  assert.equal(materialAdjustment({ customer_requirement: '', quoted_configuration: '4 核 8G' }), '');
  assert.equal(friendlyRegion('ap-northeast-1'), '东京');
});

test('removes internal cost-selection wording but preserves useful configuration facts', () => {
  assert.equal(
    simplifyCustomerText(
      '选择 Standard_B8as_v2，8 vCPU / 32 GiB，6 台，730 小时/月；在已核对的精确 8C32G Linux 候选中月费最低。',
    ),
    '选择 Standard_B8as_v2，8 vCPU / 32 GiB，6 台，730 小时/月。',
  );
  assert.equal(
    simplifyCustomerText('P10 LRS 128 GiB，共 6 块；未指定冗余方式，采用最低成本 LRS。'),
    'P10 LRS 128 GiB，共 6 块；未指定冗余方式，采用 LRS。',
  );
  assert.equal(
    simplifyCustomerText('Premium P3，26 GiB 级别，按 3 个物理节点计；主节点+2 个副本，支持自动故障切换。'),
    'Premium P3，26 GiB 级别，按 3 个物理节点计；主节点+2 个副本，支持自动故障切换。',
  );
  assert.doesNotMatch(
    simplifyCustomerText('完全匹配 8C32G，并按官方候选价格选择较低档。'),
    /最低|较低档|最便宜|候选价格/,
  );
});

test('removes an internal downsize rule without deleting customer-visible specifications', () => {
  const text = simplifyCustomerText(
    '官方完全匹配的 8C32GiB Redis 节点，按不超配规则选择最临近的小一档 cache.m6g.2xlarge。',
  );

  assert.match(text, /8C32GiB Redis/);
  assert.match(text, /cache\.m6g\.2xlarge/);
  assert.doesNotMatch(text, /不超配|小一档/);
});

test('removes a cheapest-candidate suffix without deleting its configuration', () => {
  const text = simplifyCustomerText(
    'Standard_B8as_v2，8 vCPU / 32 GiB，6 台，在已核对候选中月费最低。',
  );

  assert.match(text, /Standard_B8as_v2/);
  assert.match(text, /8 vCPU \/ 32 GiB/);
  assert.doesNotMatch(text, /候选|月费最低/);
});

test('uses the full requested quantity in each GPT-supplied amortized scenario amount', () => {
  const record = verifiedRecord();
  record.pricing_scenarios = [
    { scenario_key: 'on_demand', monthly_total: '742.92', upfront_total: '0' },
    { scenario_key: 'one_year_commitment', monthly_total: '436.75', upfront_total: '5241.00' },
  ];
  record.resource_ir[0].customer_facing.quantity = '3 台';
  record.resource_ir[0].customer_facing.reference_unit_price = '按需 $0.3392/台小时；1 年全预付 $1,747/台';
  record.resource_ir[0].scenario_costs = [
    { scenario_key: 'on_demand', monthly_cost: '742.92', upfront_cost: '0' },
    { scenario_key: 'one_year_commitment', monthly_cost: '436.75', upfront_cost: '5241.00' },
  ];

  const rows = componentDetails(record);
  assert.equal(rows[0][4], '3 台');
  assert.equal(rows[0][6], 742.92);
  assert.equal(rows[0][7], 436.75);
  assert.equal(rows[0][8], '按需 $0.3392/台小时；1 年全预付 $1,747/台');
});
