<?php

use App\Models\Export;
use Illuminate\Support\Facades\Schedule;
use Illuminate\Support\Facades\Storage;

Schedule::call(function () {
    Export::where('created_at', '<', now()->subDays(7))->each(function (Export $export) {
        if ($export->path) {
            Storage::disk('exports')->delete($export->path);
        }
        $export->delete();
    });
})->daily()->name('prune-exports')->withoutOverlapping();
