// 提问输入条。

import { useState, type FormEvent } from 'react';

interface Props {
  onSubmit: (question: string) => void;
  disabled?: boolean;
  placeholder?: string;
}

export function QueryBar({ onSubmit, disabled, placeholder }: Props) {
  const [value, setValue] = useState('');

  const handleSubmit = (event: FormEvent): void => {
    event.preventDefault();
    const question = value.trim();
    if (!question) return;
    onSubmit(question);
    setValue('');
  };

  return (
    <form className="query-bar" onSubmit={handleSubmit}>
      <input
        type="text"
        value={value}
        disabled={disabled}
        placeholder={placeholder ?? '例如：华东上个月 GMV 多少？'}
        onChange={(event) => setValue(event.target.value)}
      />
      <button type="submit" disabled={disabled || !value.trim()}>
        提问
      </button>
    </form>
  );
}
