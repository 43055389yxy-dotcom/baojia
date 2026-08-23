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
  className?: string;
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

export function ConfigurationOptionPicker({ options, value, onChange, catalog = false, className = "" }: Props) {
  const [vcpu, setVcpu] = useState("");
  const [memory, setMemory] = useState("");
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
    onChange("");
  };

  if (!catalog) {
    return (
      <div className={className}>
        {options.map((option) => (
          <button type="button" className={value === option.value ? "selected" : ""} key={option.value} onClick={() => onChange(option.value)}>
            {option.label}
          </button>
        ))}
      </div>
    );
  }

  return (
    <div className={`${className} configuration-picker`}>
      {hasProcessorFilter && <div className="configuration-picker-filters">
        <label>
          <span>1. 选择处理器</span>
          <select aria-label="选择处理器" value={vcpu} onChange={(event) => handleVcpuChange(event.target.value)}>
            <option value="">请选择 vCPU</option>
              {vcpuValues.map((item) => <option value={item} key={item}>{item} vCPU</option>)}
          </select>
        </label>
        {vcpu && memoryValues.length > 0 && <label>
          <span>2. 选择内存</span>
          <select aria-label="选择内存" value={memory} onChange={(event) => handleMemoryChange(event.target.value)}>
            <option value="">请选择内存</option>
              {memoryValues.map((item) => <option value={item} key={item}>{item} GiB</option>)}
          </select>
        </label>}
      </div>}
      {hasProcessorFilter && !vcpu ? (
        <div className="configuration-picker-hint">请先选择处理器，系统会自动显示对应的内存和官方型号。</div>
      ) : hasProcessorFilter && memoryValues.length > 0 && !memory ? (
        <div className="configuration-picker-hint">请选择该处理器支持的内存规格。</div>
      ) : filtered.length === 0 ? (
        <div className="configuration-picker-empty" role="status">当前区域没有同时满足所选处理器和内存的配置，请调整筛选条件。</div>
      ) : filtered.length === 1 ? (
        <button type="button" className={`configuration-picker-single ${value === filtered[0].option.value ? "selected" : ""}`} onClick={() => onChange(filtered[0].option.value)}>
          <small>唯一匹配配置</small><strong>{filtered[0].option.label}</strong>
        </button>
      ) : (
        <label className="configuration-picker-result">
          {hasProcessorFilter && <span>3. 选择官方型号</span>}
          <select className="configuration-picker-select" aria-label="选择可用配置" value={filtered.some(({ option }) => option.value === value) ? value : ""} onChange={(event) => onChange(event.target.value)}>
            <option value="">请选择当前区域支持的{hasProcessorFilter ? "型号" : "选项"}</option>
            {filtered.map(({ option }) => <option value={option.value} key={option.value}>{option.label}</option>)}
          </select>
        </label>
      )}
      {specificationSelectionComplete && <small className="configuration-picker-count">当前可选 {filtered.length} 项，共 {options.length} 项官方配置</small>}
    </div>
  );
}
