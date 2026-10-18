<?php

use App\Http\Controllers\ExportController;
use Illuminate\Support\Facades\Route;

Route::middleware(['auth:sanctum', 'tenant'])->group(function () {
    Route::get('/exports', [ExportController::class, 'index']);
    Route::post('/exports', [ExportController::class, 'store'])->middleware('throttle:exports');
    Route::get('/exports/{export}', [ExportController::class, 'show']);
    Route::get('/exports/{export}/download', [ExportController::class, 'download']);
    Route::delete('/exports/{export}', [ExportController::class, 'destroy']);
});
