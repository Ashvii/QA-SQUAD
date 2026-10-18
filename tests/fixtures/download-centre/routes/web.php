<?php

use Illuminate\Support\Facades\Route;
use Inertia\Inertia;

Route::middleware('auth')->group(function () {
    Route::get('/download-centre', fn () => Inertia::render('DownloadCentre'));
});
