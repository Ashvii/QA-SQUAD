<?php

namespace App\Jobs;

use App\Models\Export;
use App\Models\Invoice;
use App\Notifications\ExportReady;
use Illuminate\Bus\Queueable;
use Illuminate\Contracts\Queue\ShouldQueue;
use Illuminate\Foundation\Bus\Dispatchable;
use Illuminate\Queue\InteractsWithQueue;
use Illuminate\Queue\SerializesModels;
use Illuminate\Support\Facades\Storage;

class GenerateExportJob implements ShouldQueue
{
    use Dispatchable, InteractsWithQueue, Queueable, SerializesModels;

    public int $tries = 3;

    public int $timeout = 120;

    public function __construct(public Export $export)
    {
    }

    public function handle(): void
    {
        $this->export->update(['status' => 'processing']);

        $filters = $this->export->filters;

        $invoices = Invoice::query()
            ->whereBetween('issued_at', [$filters['from'], $filters['to']])
            ->orderBy('issued_at')
            ->get();

        $path = "{$this->export->tenant_id}/export-{$this->export->id}.csv";
        $disk = Storage::disk('exports');
        $disk->makeDirectory((string) $this->export->tenant_id);

        $handle = fopen($disk->path($path), 'a');
        fputcsv($handle, ['number', 'customer', 'issued_at', 'amount']);

        foreach ($invoices as $invoice) {
            fputcsv($handle, [
                $invoice->number,
                $invoice->customer_name,
                $invoice->issued_at->toDateString(),
                $invoice->amount,
            ]);
        }

        fclose($handle);

        $this->export->update([
            'status' => 'ready',
            'path' => $path,
            'row_count' => $invoices->count(),
        ]);

        $this->export->user->notify(new ExportReady($this->export));
    }
}
