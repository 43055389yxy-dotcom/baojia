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
  const [keyword, setKeyword] = useState("");
  const [vcpu, setVcpu] = useState("");
  const [memory, setMemory] = useState("");
  const richOptions = useMemo(() => options.map((option) => ({
    option,
    vcpu: numericSpecification(option, ["vCPU", "vcpu", "vcpus"]),
    memory: numericSpecification(option, ["memoryGiB", "memory_gib", "memory"]),
  })), [options]);
  const vcpuValues = useMemo(() => uniqueNumbers(richOptions.map((item) => item.vcpu)), [richOptions]);
  const memoryValues = useMemo(() => uniqueNumbers(richOptions.map((item) => item.memory)), [richOptions]);
  const hasSpecificationFilters = vcpuValues.length > 0 || memoryValues.length > 0;
  const filtered = useMemo(() => {
    const normalizedKeyword = keyword.trim().toLocaleLowerCase();
    const selected = richOptions.find((item) => item.option.value === value);
    const matches = richOptions.filter((item) => {
      const keywordMatches = !normalizedKeyword || `${item.option.model ?? ""} ${item.option.label}`.toLocaleLowerCase().includes(normalizedKeyword);
      const vcpuMatches = !vcpu || item.vcpu === Number(vcpu);
      const memoryMatches = !memory || item.memory === Number(memory);
      return keywordMatches && vcpuMatches && memoryMatches;
    });
    if (selected && !matches.some((item) => item.option.value === selected.option.value)) return [selected, ...matches];
    return matches;
  }, [keyword, memory, richOptions, value, vcpu]);

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
      <div className="configuration-picker-filters">
        <input aria-label="筛选可用项" value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder={hasSpecificationFilters ? "搜索型号或配置" : "搜索可用项"} />
        {vcpuValues.length > 0 && <select aria-label="按处理器筛选" value={vcpu} onChange={(event) => setVcpu(event.target.value)}>
          <option value="">全部处理器</option>
            {vcpuValues.map((item) => <option value={item} key={item}>{item} vCPU</option>)}
        </select>}
        {memoryValues.length > 0 && <select aria-label="按内存筛选" value={memory} onChange={(event) => setMemory(event.target.value)}>
          <option value="">全部内存</option>
            {memoryValues.map((item) => <option value={item} key={item}>{item} GiB</option>)}
        </select>}
      </div>
      <select className="configuration-picker-select" aria-label="选择可用配置" value={value ?? ""} onChange={(event) => onChange(event.target.value)}>
        <option value="">请选择当前区域支持的{hasSpecificationFilters ? "配置" : "选项"}</option>
        {filtered.map(({ option }) => <option value={option.value} key={option.value}>{option.label}</option>)}
      </select>
      <small className="configuration-picker-count">当前显示 {filtered.length} 项，共 {options.length} 项可用配置</small>
    </div>
  );
}
