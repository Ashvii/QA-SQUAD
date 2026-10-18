<?php

namespace App\Http\Middleware;

use Closure;
use Illuminate\Http\Request;

class SetTenant
{
    public function handle(Request $request, Closure $next)
    {
        if ($user = $request->user()) {
            app()->instance('currentTenantId', $user->tenant_id);
        }

        return $next($request);
    }
}
