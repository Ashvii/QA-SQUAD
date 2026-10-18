<?php

use App\Jobs\GenerateExportJob;
use App\Models\User;
use Illuminate\Support\Facades\Queue;

it('queues an export', function () {
    Queue::fake();
    $user = User::factory()->create();

    $this->actingAs($user)
        ->postJson('/api/exports', ['type' => 'invoices', 'filters' => ['from' => '2026-01-01', 'to' => '2026-01-31']])
        ->assertStatus(202);

    Queue::assertPushed(GenerateExportJob::class);
});
