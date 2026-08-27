"use client";

import { useMemo, useState } from "react";

export type ConfigurationChoice = {
  label: string;
  value: string;
  model?: string | null;
  specifications?: Record<string, unknown>;
  monthly_catalog_cost?: number | null;
};

type Props = {
  options: ConfigurationChoice[];
  value?: string;
  onChange: (value: string) => void;
  catalog?: boolean;
  requireMachineCount?: boolean;
  initialMachineCount?: number;
  className?: string;
  placeholder?: string;
};

function numericSpecification(option: ConfigurationChoice, keys: string[]): number | null {
  const specifications = option.specifications ?? {};
  for (const key of keys) {
    const value = specifications[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  }
  return null;
}

function uniqueNumbers(values: Array<number | null>): number[] {
  return [...new Set(values.filter((value): value is number => value !== null))].sort((left, right) => left - right);
}

export function ConfigurationOptionPicker({
  options,
  value,
  onChange,
  catalog = false,
  requireMachineCount = false,
  initialMachineCount = 1,
  className = "",
  placeholder,
}: Props) {
  const [vcpu, setVcpu] = useState("");
  const [memory, setMemory] = useState("");
  const [machineCount, setMachineCount] = useState(String(Math.max(initialMachineCount, 1)));
  const selectedValue = (value ?? "").replace(/；机器(?:数量|台数)\s*\d+$/, "");
  const emitSelection = (optionValue: string, count = machineCount) => {
    if (!optionValue) {
      onChange("");
      return;
    }
    onChange(requireMachineCount ? `${optionValue}；机器数量 ${Math.max(Number(count) || 1, 1)}` : optionValue);
  };
  const richOptions = useMemo(() => options.map((option) => ({
    option,
    vcpu: numericSpecification(option, ["vCPU", "vcpu", "vcpus"]),
    memory: numericSpecification(option, ["memoryGiB", "memory_gib", "memory"]),
  })), [options]);
  const vcpuValues = useMemo(() => uniqueNumbers(
    richOptions.map((item) => item.vcpu),
  ), [richOptions]);
  const memoryValues = useMemo(() => uniqueNumbers(
    richOptions
      .filter((item) => vcpu && item.vcpu === Number(vcpu))
      .map((item) => item.memory),
  ), [richOptions, vcpu]);
  const hasProcessorFilter = vcpuValues.length > 0;
  const specificationSelectionComplete = !hasProcessorFilter || (
    Boolean(vcpu) && (memoryValues.length === 0 || Boolean(memory))
  );
  const filtered = useMemo(() => {
    return richOptions.filter((item) => {
      const vcpuMatches = !vcpu || item.vcpu === Number(vcpu);
      const memoryMatches = !memory || item.memory === Number(memory);
      return vcpuMatches && memoryMatches;
    });
  }, [memory, richOptions, vcpu]);

  const handleVcpuChange = (nextVcpu: string) => {
    setVcpu(nextVcpu);
    setMemory("");
    onChange("");
  };

  const handleMemoryChange = (nextMemory: string) => {
    setMemory(nextMemory);
    const matching = richOptions.filter((item) => (
      item.vcpu === Number(vcpu) && item.memory === Number(nextMemory)
    ));
    const cheapest = [...matching].sort((left, right) => {
      const leftCost = left.option.monthly_catalog_cost;
      const rightCost = right.option.monthly_catalog_cost;
      if (typeof leftCost === "number" && typeof rightCost === "number") return leftCost - rightCost;
      if (typeof leftCost === "number") return -1;
      if (typeof rightCost === "number") return 1;
      return 0;
    })[0];
    emitSelection(cheapest?.option.value ?? "");
  };

  const handleMachineCountChange = (nextCount: string) => {
    const normalized = nextCount.replace(/\D/g, "");
    setMachineCount(normalized);
    if (selectedValue && normalized) emitSelection(selectedValue, normalized);
    else onChange("");
  };

  if (!catalog) {
    const buttonValue = (value ?? "").split("；", 1)[0];
    return (
      <label className={`${className} configuration-picker-result`.trim()}>
        <span>请选择</span>
        <select
          className="configuration-picker-select"
          aria-label="选择确认项"
          value={options.some((option) => option.value === buttonValue) ? buttonValue : ""}
          onChange={(event) => onChange(event.target.value)}
        >
          <option value="">请选择一个选项</option>
          {options.map((option) => (
            <option value={option.value} key={option.value}>{option.label}</option>
          ))}
        </select>
      </label>
    );
  }

  return (
    <div className={`${className} configuration-picker`}>
      {requireMachineCount && <label className="configuration-picker-machine-count">
        <span>机器台数</span>
        <input
          aria-label="填写自建机器台数"
          inputMode="numeric"
          min="1"
          type="number"
          value={machineCount}
          onChange={(event) => handleMachineCountChange(event.target.value)}
        />
      </label>}
      {hasProcessorFilter && <div className="configuration-picker-filters">
        <label>
          <span>处理器</span>
          <select aria-label="选择处理器" value={vcpu} onChange={(event) => handleVcpuChange(event.target.value)}>
            <option value="">请选择 vCPU</option>
              {vcpuValues.map((item) => <option value={item} key={item}>{item} vCPU</option>)}
          </select>
        </label>
        <label>
          <span>内存</span>
          <select aria-label="选择内存" value={memory} disabled={!vcpu || memoryValues.length === 0} onChange={(event) => handleMemoryChange(event.target.value)}>
            <option value="">{!vcpu ? "请先选择处理器" : memoryValues.length === 0 ? "该处理器无需选择内存" : "请选择内存"}</option>
              {memoryValues.map((item) => <option value={item} key={item}>{item} GiB</option>)}
          </select>
        </label>
      </div>}
      {hasProcessorFilter && !vcpu ? null : hasProcessorFilter && memoryValues.length > 0 && !memory ? null : filtered.length === 0 ? (
        <div className="configuration-picker-empty" role="status">当前区域没有同时满足所选处理器和内存的配置，请调整筛选条件。</div>
      ) : filtered.length === 1 ? (
        <button type="button" className={`configuration-picker-single ${selectedValue === filtered[0].option.value ? "selected" : ""}`} onClick={() => emitSelection(filtered[0].option.value)}>
          <small>唯一匹配配置</small><strong>{filtered[0].option.label}</strong>
        </button>
      ) : (
        <label className="configuration-picker-result">
          {hasProcessorFilter && <span>官方型号</span>}
          <select className="configuration-picker-select" aria-label="选择可用配置" value={filtered.some(({ option }) => option.value === selectedValue) ? selectedValue : ""} onChange={(event) => emitSelection(event.target.value)}>
            <option value="">{placeholder ?? `请选择当前区域支持的${hasProcessorFilter ? "型号" : "选项"}`}</option>
            {filtered.map(({ option }) => <option value={option.value} key={option.value}>{option.label}</option>)}
          </select>
        </label>
      )}
      {specificationSelectionComplete && <small className="configuration-picker-count">当前可选 {filtered.length} 项，共 {options.length} 项官方配置</small>}
    </div>
  );
}
