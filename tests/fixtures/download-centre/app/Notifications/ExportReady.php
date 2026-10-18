<?php

namespace App\Notifications;

use App\Models\Export;
use Illuminate\Notifications\Messages\MailMessage;
use Illuminate\Notifications\Notification;

class ExportReady extends Notification
{
    public function __construct(public Export $export)
    {
    }

    public function via(object $notifiable): array
    {
        return ['mail'];
    }

    public function toMail(object $notifiable): MailMessage
    {
        return (new MailMessage)
            ->subject('Your export is ready')
            ->action('Download', url("/download-centre?export={$this->export->id}"));
    }
}
