<?php

namespace App\Providers;

use App\Models\Export;
use App\Policies\ExportPolicy;
use Illuminate\Cache\RateLimiting\Limit;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Gate;
use Illuminate\Support\Facades\RateLimiter;
use Illuminate\Support\ServiceProvider;

class AppServiceProvider extends ServiceProvider
{
    public function boot(): void
    {
        Gate::policy(Export::class, ExportPolicy::class);

        RateLimiter::for('exports', function (Request $request) {
            return Limit::perMinute(5)->by($request->user()->id);
        });
    }
}
