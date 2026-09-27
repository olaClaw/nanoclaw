/**
 * Minimal strict schema language for the dashboard API contract.
 *
 * Every response the dashboard sends is described here and checked against
 * its schema before it leaves the host boundary. Objects are exact: every
 * declared field is required (use `nullable` for absent values) and any extra
 * field is an error, so a raw row or CLI result can never slip through with
 * more fields than the contract allows. Strings are always length-bounded and
 * arrays always size-bounded.
 *
 * No dependency on purpose: the contract must stay auditable in one file.
 */

export type Schema =
  | { readonly kind: 'string'; readonly maxLength: number; readonly pattern?: RegExp }
  | { readonly kind: 'enum'; readonly values: readonly string[] }
  | { readonly kind: 'literal'; readonly value: true }
  | { readonly kind: 'integer'; readonly min: number; readonly max: number }
  | { readonly kind: 'boolean' }
  | { readonly kind: 'timestamp'; readonly precision: 'second' | 'minute' }
  | { readonly kind: 'nullable'; readonly of: Schema }
  | { readonly kind: 'array'; readonly of: Schema; readonly maxItems: number }
  | { readonly kind: 'object'; readonly fields: Readonly<Record<string, Schema>> };

/** Visible, printable text: no control characters, no line breaks. */
const PRINTABLE = /^[^\p{Cc}\p{Cf}\p{Zl}\p{Zp}]*$/u;
const SECOND_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/;
const MINUTE_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00Z$/;

export const str = (maxLength: number, pattern?: RegExp) => ({ kind: 'string', maxLength, pattern }) as const;
export const oneOf = <const V extends readonly string[]>(...values: V) => ({ kind: 'enum', values }) as const;
export const confirmed = { kind: 'literal', value: true } as const;
export const int = (min: number, max: number) => ({ kind: 'integer', min, max }) as const;
export const bool = { kind: 'boolean' } as const;
/** ISO-8601 UTC with `Z`, the storage format used everywhere in NanoClaw. */
export const timestamp = { kind: 'timestamp', precision: 'second' } as const;
/** Activity times are rounded down to the minute before they leave the host. */
export const minuteTimestamp = { kind: 'timestamp', precision: 'minute' } as const;
export const nullable = <S extends Schema>(of: S) => ({ kind: 'nullable', of }) as const;
export const array = <S extends Schema>(of: S, maxItems: number) => ({ kind: 'array', of, maxItems }) as const;
export const object = <F extends Record<string, Schema>>(fields: F) => ({ kind: 'object', fields }) as const;

type Prettify<T> = { [K in keyof T]: T[K] } & {};

/** TypeScript type of a value that satisfies a schema. */
export type Infer<S> = S extends { kind: 'string' }
  ? string
  : S extends { kind: 'enum'; values: readonly (infer V)[] }
    ? V
    : S extends { kind: 'literal' }
      ? true
      : S extends { kind: 'integer' }
        ? number
        : S extends { kind: 'boolean' }
          ? boolean
          : S extends { kind: 'timestamp' }
            ? string
            : S extends { kind: 'nullable'; of: infer O }
              ? Infer<O> | null
              : S extends { kind: 'array'; of: infer O }
                ? Infer<O>[]
                : S extends { kind: 'object'; fields: infer F }
                  ? Prettify<{ -readonly [K in keyof F]: Infer<F[K]> }>
                  : never;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Validate a value. Returns error locations only (`$.items[2].label: too_long`);
 * never the offending value, so the result is safe to log.
 */
export function validate(schema: Schema, value: unknown, at = '$'): string[] {
  switch (schema.kind) {
    case 'string':
      if (typeof value !== 'string') return [`${at}: not_string`];
      if (value.length > schema.maxLength) return [`${at}: too_long`];
      if (!PRINTABLE.test(value)) return [`${at}: not_printable`];
      if (schema.pattern && !schema.pattern.test(value)) return [`${at}: pattern`];
      return [];
    case 'enum':
      return typeof value === 'string' && schema.values.includes(value) ? [] : [`${at}: not_allowed`];
    case 'literal':
      return value === schema.value ? [] : [`${at}: not_confirmed`];
    case 'integer':
      return Number.isSafeInteger(value) && (value as number) >= schema.min && (value as number) <= schema.max
        ? []
        : [`${at}: integer_range`];
    case 'boolean':
      return typeof value === 'boolean' ? [] : [`${at}: not_boolean`];
    case 'timestamp': {
      const shape = schema.precision === 'minute' ? MINUTE_TIMESTAMP : SECOND_TIMESTAMP;
      return typeof value === 'string' && shape.test(value) && !Number.isNaN(Date.parse(value))
        ? []
        : [`${at}: timestamp`];
    }
    case 'nullable':
      return value === null ? [] : validate(schema.of, value, at);
    case 'array': {
      if (!Array.isArray(value)) return [`${at}: not_array`];
      if (value.length > schema.maxItems) return [`${at}: too_many_items`];
      return value.flatMap((item, index) => validate(schema.of, item, `${at}[${index}]`));
    }
    case 'object': {
      if (!isRecord(value)) return [`${at}: not_object`];
      const errors: string[] = [];
      for (const key of Object.keys(value)) {
        if (!Object.hasOwn(schema.fields, key)) errors.push(`${at}: unexpected_field`);
      }
      for (const [key, field] of Object.entries(schema.fields)) {
        if (!Object.hasOwn(value, key)) errors.push(`${at}.${key}: missing`);
        else errors.push(...validate(field, value[key], `${at}.${key}`));
      }
      return errors;
    }
  }
}

/** Every field name a schema can emit, with its path; used by the contract tests. */
export function fieldPaths(schema: Schema, at = '$'): string[] {
  switch (schema.kind) {
    case 'nullable':
      return fieldPaths(schema.of, at);
    case 'array':
      return fieldPaths(schema.of, `${at}[]`);
    case 'object':
      return Object.entries(schema.fields).flatMap(([key, field]) => [
        `${at}.${key}`,
        ...fieldPaths(field, `${at}.${key}`),
      ]);
    default:
      return [];
  }
}
