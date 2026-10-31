import React, { createContext, useContext } from "react";

export type BadgeVariant = "neutral" | "danger" | "warning" | "success" | "information" | "discovery" | "planet" | "ai";
type Visual = { variant?: string; emphasis?: string; scale?: string; };
type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & Visual & { label?: string; icon?: string; iconName?: string; };
function Icon({ name }: { name?: string }) {
  return name ? <span className="material-icons" aria-hidden="true">{name}</span> : null;
}
export function UiButton({ label, icon, iconName, variant, emphasis, scale, children, className = "", ...props }: ButtonProps) {
  return <button type="button" {...props} className={`ui-button ${className}`} data-variant={variant} data-emphasis={emphasis} data-scale={scale}><Icon name={icon ?? iconName} />{label ?? children}</button>;
}
export function UiIconButton(props: ButtonProps) { return <UiButton {...props} />; }
type FormProps = Omit<React.InputHTMLAttributes<HTMLInputElement>, "onChange"> & {
  label?: string; labelKind?: string; isMultiline?: boolean; rows?: number;
  onChange?: (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => void;
};
export function UiFormItem({ label, labelKind: _labelKind, isMultiline, rows, id, className = "", onChange, ...props }: FormProps) {
  return <label className={`ui-field ${className}`} htmlFor={id}>{label && <span>{label}</span>}{isMultiline
    ? <textarea {...props as React.TextareaHTMLAttributes<HTMLTextAreaElement>} id={id} rows={rows} onChange={onChange} />
    : <input {...props} id={id} onChange={onChange} />}</label>;
}
type SearchProps = Omit<React.InputHTMLAttributes<HTMLInputElement>, "onChange"> & Visual & { inputRef?: React.Ref<HTMLInputElement>; onChange?: (event: React.ChangeEvent<HTMLInputElement>, value: string) => void; };
export function UiInputSearch({ onChange, inputRef, scale: _scale, variant: _variant, emphasis: _emphasis, className = "", ...props }: SearchProps) {
  return <input {...props} ref={inputRef} className={`ui-search ${className}`} type="search" onChange={e => onChange?.(e, e.target.value)} />;
}
type CheckboxProps = Omit<React.InputHTMLAttributes<HTMLInputElement>, "onChange"> & { label?: React.ReactNode; onChange?: (event: React.ChangeEvent<HTMLInputElement>, checked: boolean) => void; };
export function UiCheckbox({ label, children, onChange, className = "", ...props }: CheckboxProps) {
  return <label className={`ui-checkbox ${className}`}><input {...props} type="checkbox" onChange={e => onChange?.(e, e.target.checked)} /><span>{label ?? children}</span></label>;
}
export function UiBadge({ children, variant, emphasis: _emphasis, scale: _scale, isUppercase, className = "", ...props }: React.HTMLAttributes<HTMLSpanElement> & Visual & { isUppercase?: boolean }) {
  return <span {...props} className={`ui-badge ${className}`} data-variant={variant} style={{textTransform: isUppercase ? "uppercase" : undefined, ...props.style}}>{children}</span>;
}
const Segments = createContext<{ value?: string; onChange?: (value: string) => void }>({});
type SegmentProps = Omit<React.HTMLAttributes<HTMLDivElement>, "onChange"> & Visual & { value?: string; onChange?: (value: string) => void; };
export function UiSegmentedControl({ value, onChange, children, className = "", scale: _scale, variant: _variant, emphasis: _emphasis, ...props }: SegmentProps) {
  return <Segments.Provider value={{value, onChange}}><div {...props} className={`ui-segments ${className}`} role="group">{children}</div></Segments.Provider>;
}
export function UiSegmentedControlButton({ value, iconName, children, ...props }: ButtonProps) {
  const group = useContext(Segments);
  return <button {...props} type="button" aria-pressed={group.value === value} onClick={() => group.onChange?.(String(value))}><Icon name={iconName} />{children}</button>;
}
type SliderProps = Omit<React.InputHTMLAttributes<HTMLInputElement>, "onChange"> & { label?: string; onChange?: (value: number) => void; };
export function UiSlider({ label, onChange, className = "", ...props }: SliderProps) {
  return <label className={`ui-field ${className}`}>{label}<input {...props} type="range" onChange={e => onChange?.(Number(e.target.value))} /></label>;
}
export function UiAccordion({ children, isSeparated: _isSeparated, withoutBorder: _withoutBorder, ...props }: React.HTMLAttributes<HTMLDivElement> & { isSeparated?: boolean; withoutBorder?: boolean }) { return <div {...props}>{children}</div>; }
export function UiAccordionItem(props: React.DetailsHTMLAttributes<HTMLDetailsElement>) { return <details {...props} />; }
export function UiAccordionItemHeader(props: React.HTMLAttributes<HTMLElement>) { return <summary {...props} />; }
export function UiAccordionItemContent(props: React.HTMLAttributes<HTMLDivElement>) { return <div {...props} />; }
export function UiHeading({ as: Tag = "h2", scale: _scale, ...props }: React.HTMLAttributes<HTMLHeadingElement> & { as?: "h1" | "h2" | "h3" | "h4" | "h5" | "h6"; scale?: string }) { return <Tag {...props} />; }
export function UiSpinner(props: React.HTMLAttributes<HTMLSpanElement>) { return <span role="status" {...props}>…</span>; }
