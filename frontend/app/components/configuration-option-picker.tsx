"use client";

import { useMemo, useState } from "react";

export type ConfigurationChoice = {
  label: string;
  value: string;
  description?: string | null;
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
  initialVcpu?: number;
  initialMemoryGiB?: number;
  architecturePreference?: "arm64" | "x86_64";
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

function optionArchitecture(option: ConfigurationChoice): "arm64" | "x86_64" | null {
  const declared = option.specifications?.processorArchitecture
    ?? option.specifications?.processor_architecture;
  const plural = option.specifications?.processorArchitectures
    ?? option.specifications?.architectures;
  const declaredValues = [
    ...(Array.isArray(plural) ? plural : plural === undefined ? [] : [plural]),
    ...(declared === undefined ? [] : [declared]),
  ];
  for (const value of declaredValues) {
    if (typeof value !== "string") continue;
    const normalized = value.trim().toLowerCase();
    if (["arm", "arm64", "aarch64", "graviton"].includes(normalized)) return "arm64";
    if (["x86", "x86_64", "amd64", "i386"].includes(normalized)) return "x86_64";
  }
  const model = (option.model ?? "").trim().toLowerCase();
  if (!model.includes(".")) return null;
  const segments = model.split(".");
  if (segments.some((segment) => segment === "a1" || segment === "mac2" || segment.startsWith("mac2-"))) return "arm64";
  if (segments.some((segment) => /\d+g[a-z]*$/.test(segment))) return "arm64";
  return "x86_64";
}

function modelConfigurationLabel(option: ConfigurationChoice): string {
  const vcpu = numericSpecification(option, ["vCPU", "vcpu", "vcpus"]);
  const memory = numericSpecification(option, ["memoryGiB", "memory_gib", "memory"]);
  return [
    option.model ?? option.label,
    vcpu === null ? null : `${vcpu} vCPU`,
    memory === null ? null : `${memory} GiB`,
  ].filter(Boolean).join(" · ");
}

export function ConfigurationOptionPicker({
  options,
  value,
  onChange,
  catalog = false,
  requireMachineCount = false,
  initialMachineCount = 1,
  initialVcpu,
  initialMemoryGiB,
  architecturePreference,
  className = "",
  placeholder,
}: Props) {
  const selectedValue = (value ?? "").replace(/；机器(?:数量|台数)\s*\d+$/, "");
  const selectedMachineCount = Number(
    (value ?? "").match(/；机器(?:数量|台数)\s*(\d+)$/)?.[1],
  );
  const richOptions = useMemo(() => options.map((option) => ({
    option,
    vcpu: numericSpecification(option, ["vCPU", "vcpu", "vcpus"]),
    memory: numericSpecification(option, ["memoryGiB", "memory_gib", "memory"]),
    architecture: optionArchitecture(option),
  })), [options]);
  const architectureMatches = useMemo(() => (
    architecturePreference
      ? richOptions.filter((item) => item.architecture === architecturePreference)
      : richOptions
  ), [architecturePreference, richOptions]);
  // ARM is the cost-oriented default, but a component that AWS exposes only
  // on x86 must remain selectable. An explicit x86 choice is strict and never
  // falls back to ARM.
  const architectureFallback = architecturePreference === "arm64"
    && architectureMatches.length === 0
    && richOptions.length > 0;
  const architectureCatalog = architectureFallback ? richOptions : architectureMatches;
  const [machineCount, setMachineCount] = useState(String(Math.max(
    Number.isFinite(selectedMachineCount) && selectedMachineCount > 0
      ? selectedMachineCount
      : initialMachineCount,
    1,
  )));
  const emitSelection = (optionValue: string, count = machineCount) => {
    if (!optionValue) {
      onChange("");
      return;
    }
    onChange(requireMachineCount ? `${optionValue}；机器数量 ${Math.max(Number(count) || 1, 1)}` : optionValue);
  };
  const visibleOptions = useMemo(() => {
    const hasRequestedShape = Boolean(initialVcpu && initialMemoryGiB);
    const distance = (item: (typeof architectureCatalog)[number]) => {
      if (!hasRequestedShape || item.vcpu === null || item.memory === null) return Number.POSITIVE_INFINITY;
      return Math.abs(Math.log2(Math.max(item.vcpu, 0.001) / Number(initialVcpu)))
        + Math.abs(Math.log2(Math.max(item.memory, 0.001) / Number(initialMemoryGiB)));
    };
    const ranked = [...architectureCatalog].sort((left, right) => (
      distance(left) - distance(right)
      || Number(right.option.specifications?.exactRequestedShape === true)
        - Number(left.option.specifications?.exactRequestedShape === true)
      || (left.option.model ?? left.option.label).localeCompare(
        right.option.model ?? right.option.label,
      )
    ));
    if (!hasRequestedShape) {
      return ranked.slice(0, 10).sort((left, right) => (
        (left.vcpu ?? Number.POSITIVE_INFINITY) - (right.vcpu ?? Number.POSITIVE_INFINITY)
        || (left.memory ?? Number.POSITIVE_INFINITY) - (right.memory ?? Number.POSITIVE_INFINITY)
        || (left.option.model ?? left.option.label).localeCompare(
          right.option.model ?? right.option.label,
        )
      ));
    }
    // "Lower" means at least one official dimension is below the customer's
    // request. Everything else is an exact-or-upward option. Keep five from
    // each side when available, then fill any vacant slots from the nearest
    // remaining official models so the dropdown never exceeds ten entries.
    const lowerRanked = ranked.filter((item) => (
      item.vcpu !== null
      && item.memory !== null
      && (item.vcpu < Number(initialVcpu) || item.memory < Number(initialMemoryGiB))
    ));
    const upperRanked = ranked.filter((item) => (
      item.vcpu !== null
      && item.memory !== null
      && item.vcpu >= Number(initialVcpu)
      && item.memory >= Number(initialMemoryGiB)
    ));
    const lower = lowerRanked.slice(0, 5);
    const upper = upperRanked.slice(0, 5);
    const selectedValues = new Set([...upper, ...lower].map((item) => item.option.value));
    for (const item of ranked) {
      if (selectedValues.size >= 10 || selectedValues.has(item.option.value)) continue;
      selectedValues.add(item.option.value);
      if (lowerRanked.includes(item)) lower.push(item);
      else upper.push(item);
    }
    const combined = [...upper, ...lower];
    const selectedOption = architectureCatalog.find((item) => item.option.value === selectedValue);
    if (selectedOption && !combined.some((item) => item.option.value === selectedValue)) {
      combined.unshift(selectedOption);
    }
    return combined.slice(0, 10).sort((left, right) => (
      (left.vcpu ?? Number.POSITIVE_INFINITY) - (right.vcpu ?? Number.POSITIVE_INFINITY)
      || (left.memory ?? Number.POSITIVE_INFINITY) - (right.memory ?? Number.POSITIVE_INFINITY)
      || (left.option.model ?? left.option.label).localeCompare(
        right.option.model ?? right.option.label,
      )
    ));
  }, [architectureCatalog, initialMemoryGiB, initialVcpu, selectedValue]);
  const visibleValues = new Set(visibleOptions.map((item) => item.option.value));

  const handleMachineCountChange = (nextCount: string) => {
    const normalized = nextCount.replace(/\D/g, "");
    setMachineCount(normalized);
    if (selectedValue && normalized) emitSelection(selectedValue, normalized);
    else onChange("");
  };

  if (!catalog) {
    const buttonValue = (value ?? "").split("；", 1)[0];
    return (
      <div className={`${className} configuration-picker-choice-wrap`.trim()}>
        <div className="configuration-picker-choice-heading">
          <strong>选择一个方案</strong>
          <span>点击下面的卡片即可选择</span>
        </div>
        <div className="configuration-picker-explanations" role="radiogroup" aria-label="选择确认方案">
          {options.map((option) => {
            const selected = buttonValue === option.value;
            return (
            <button
              type="button"
              key={`explanation-${option.value}`}
              role="radio"
              aria-checked={selected}
              className={selected ? "selected" : ""}
              onClick={() => onChange(option.value)}
            >
              <span className="configuration-picker-choice-mark" aria-hidden="true">{selected ? "✓" : ""}</span>
              <span className="configuration-picker-choice-copy">
                <strong>{option.label}</strong>
                {option.description && <span>{option.description}</span>}
              </span>
            </button>
            );
          })}
        </div>
      </div>
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
      {architectureFallback && <div className="configuration-picker-architecture-note" role="status">
        该组件当前没有ARM型号，已显示AWS提供的其他官方型号。
      </div>}
      {visibleOptions.length === 0 ? (
        <div className="configuration-picker-empty" role="status">当前地区和处理器架构没有可选的官方型号。</div>
      ) : <label className="configuration-picker-result">
        <span>官方型号</span>
        <select className="configuration-picker-select" aria-label="选择可用型号" value={visibleValues.has(selectedValue) ? selectedValue : ""} onChange={(event) => emitSelection(event.target.value)}>
          <option value="">{placeholder ?? "请选择型号"}</option>
          {visibleOptions.map(({ option }) => <option value={option.value} key={option.value}>
            {modelConfigurationLabel(option)}
          </option>)}
        </select>
      </label>}
    </div>
  );
}
