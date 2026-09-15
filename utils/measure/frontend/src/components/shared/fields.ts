import { html, nothing } from "lit";
import type { EntityDescriptor } from "../../types";
import type { ComboboxOption } from "./combobox";
import "./combobox";

/**
 * Form controls shared by the views that render inputs. They are plain functions rather than a
 * base class so any view can compose them, and they carry no state of their own — the caller
 * supplies the current value and receives the change through the form or an explicit handler.
 */

export interface TextFieldOptions {
  value?: string;
  placeholder?: string;
  required?: boolean;
  hint?: string;
}

export function textField(name: string, label: string, options: TextFieldOptions = {}) {
  const { value = "", placeholder = "", required = false, hint = "" } = options;
  return html`<label>
    <span>${label}</span>
    <input name=${name} .value=${value} placeholder=${placeholder} ?required=${required} autocomplete="off" />
    ${fieldHint(hint)}
  </label>`;
}

export interface NumberFieldOptions {
  min?: number;
  max?: number;
  step?: string;
  hint?: string;
  detail?: string;
  required?: boolean;
  disabled?: boolean;
  onInput?: ((event: Event) => void) | null;
}

export function numberField(name: string, label: string, value: string, options: NumberFieldOptions = {}) {
  const { min, max, step = "1", hint = "", required = true, disabled = false, onInput = null } = options;
  return html`<label>
    <span>${label}</span>
    <input
      type="number"
      name=${name}
      min=${min ?? nothing}
      max=${max ?? nothing}
      step=${step}
      .value=${value}
      ?required=${required}
      ?disabled=${disabled}
      @input=${onInput}
    />
    ${fieldHint(hint)}
  </label>`;
}

export function sliderNumberField(name: string, label: string, value: string, options: NumberFieldOptions = {}) {
  const { min, max, step = "1", hint = "", detail = "", required = true, disabled = false, onInput = null } = options;
  const sync = (event: Event) => {
    const input = event.currentTarget as HTMLInputElement;
    const wrap = input.closest(".slider-number");
    const slider = wrap?.querySelector<HTMLInputElement>('input[type="range"]');
    const number = wrap?.querySelector<HTMLInputElement>('input[type="number"]');
    if (slider && number && slider.value !== number.value) {
      if (input === slider) number.value = slider.value;
      else slider.value = number.value;
    }
    if (number && onInput) onInput({ currentTarget: number } as unknown as Event);
  };
  return html`<label class="slider-number">
    <span>${label}</span>
    <div class="slider-number-controls">
      <input
        type="range"
        min=${min ?? nothing}
        max=${max ?? nothing}
        step=${step}
        .value=${value}
        ?disabled=${disabled}
        aria-label=${label}
        @input=${sync}
      />
      <input
        type="number"
        name=${name}
        min=${min ?? nothing}
        max=${max ?? nothing}
        step=${step}
        .value=${value}
        ?required=${required}
        ?disabled=${disabled}
        @input=${sync}
      />
      ${detail ? html`<span class="slider-number-detail">${detail}</span>` : nothing}
    </div>
    ${fieldHint(hint)}
  </label>`;
}

export interface EntitySelectOptions {
  selected?: string;
  required?: boolean;
  hint?: string;
  onChange?: ((event: Event) => void) | null;
}

export interface OptionSelectOptions {
  selected?: string;
  required?: boolean;
  hint?: string;
  placeholder?: string;
  onChange?: ((event: Event) => void) | null;
}

export function optionSelect(
  name: string,
  label: string,
  options: ComboboxOption[],
  settings: OptionSelectOptions = {},
) {
  const { selected = "", required = false, hint = "", placeholder = `Select ${label.toLowerCase()}`, onChange = null } = settings;
  return html`
    <measure-combobox
      name=${name}
      label=${label}
      .value=${selected}
      .options=${options}
      placeholder=${placeholder}
      hint=${hint}
      ?required=${required}
    >
      <input slot="value" type="hidden" name=${name} .value=${selected} @change=${onChange} />
    </measure-combobox>
  `;
}

export function entitySelect(name: string, label: string, entities: EntityDescriptor[], options: EntitySelectOptions = {}) {
  const { selected = "", required = false, hint = "", onChange = null } = options;
  const comboboxOptions: ComboboxOption[] = entities.map((entity) => ({
    value: entity.entity_id,
    label: `${entity.name} · ${entity.entity_id}`,
  }));
  if (!required) comboboxOptions.unshift({ value: "", label: "None" });
  return optionSelect(name, label, comboboxOptions, {
    selected,
    required,
    hint,
    placeholder: `Search ${label.toLowerCase()} entities`,
    onChange,
  });
}

export function fieldHint(hint: string) {
  return hint ? html`<small class="field-hint">${hint}</small>` : nothing;
}
