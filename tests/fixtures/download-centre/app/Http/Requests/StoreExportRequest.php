<?php

namespace App\Http\Requests;

use Illuminate\Foundation\Http\FormRequest;

class StoreExportRequest extends FormRequest
{
    public function authorize(): bool
    {
        return $this->user() !== null;
    }

    public function rules(): array
    {
        return [
            'type' => ['required', 'in:invoices'],
            'filters' => ['required', 'array'],
            'filters.from' => ['required', 'date'],
            'filters.to' => ['required', 'date', 'after_or_equal:filters.from'],
            'filters.label' => ['nullable', 'string', 'max:100'],
        ];
    }
}
